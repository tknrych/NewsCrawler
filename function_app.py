import azure.functions as func
import logging
import os
import json
import requests
import uuid
from datetime import datetime, timezone
import traceback
import time
import markdown
import xml.etree.ElementTree as ET

from azure.cosmos import CosmosClient, exceptions
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient
from bs4 import BeautifulSoup
from openai import AzureOpenAI
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = func.FunctionApp()

# ===================================================================
# データ収集関数群
# ===================================================================
@app.schedule(schedule="0 */3 * * *", arg_name="myTimer", run_on_startup=False)
def HackerNewsCollector(myTimer: func.TimerRequest) -> None:
    logging.info('Hacker News Collector function ran.')
    try:
        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set.")
        HACKER_NEWS_API_BASE = "https://hacker-news.firebaseio.com/v0"
        TARGET_STORIES = 50
        top_stories_url = f"{HACKER_NEWS_API_BASE}/topstories.json"
        response = requests.get(top_stories_url, timeout=15, verify=False)
        response.raise_for_status()
        story_ids = response.json()[:TARGET_STORIES]
        logging.info(f"Successfully fetched {len(story_ids)} story IDs.")
        queue_client = QueueClient.from_connection_string(
            conn_str=storage_connection_string,
            queue_name=os.environ.get("QUEUE_NAME", "urls-to-summarize")
        )
        try:
            queue_client.create_queue()
        except ResourceExistsError:
            pass 
        except Exception as e:
             logging.warning(f"Could not create queue, may already exist: {e}")
             pass
             
        sent_count = 0
        for rank, story_id in enumerate(story_ids):
            story_detail_url = f"{HACKER_NEWS_API_BASE}/item/{story_id}.json"
            story_res = requests.get(story_detail_url, timeout=15)
            story_data = story_res.json()
            if story_data and "url" in story_data:
                message = {
                    "source": "HackerNews", 
                    "url": story_data["url"], 
                    "title": story_data.get("title", "No Title"),
                    "rank": rank 
                }
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                sent_count += 1
        logging.info(f"Successfully sent {sent_count} URLs to the queue.")
    except Exception as e:
        logging.error(f"--- FATAL ERROR in HackerNewsCollector ---")
        logging.error(traceback.format_exc())
        raise

@app.schedule(schedule="5 */3 * * *", arg_name="myTimer", run_on_startup=False)
def ArXivCollector(myTimer: func.TimerRequest) -> None:
    logging.info('ArXiv AI Collector function (API version) ran.')
    try:
        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set.")
        queue_client = QueueClient.from_connection_string(
            conn_str=storage_connection_string,
            queue_name=os.environ.get("QUEUE_NAME", "urls-to-summarize")
        )
        TARGET_CATEGORIES = ['cs.AI', 'cs.LG']
        BASE_API_URL = 'http://export.arxiv.org/api/query?'
        max_results = 50
        total_sent_count = 0
        for category in TARGET_CATEGORIES:
            logging.info(f"Fetching articles for category: {category}")
            search_query = f'cat:{category}'
            api_url = f'{BASE_API_URL}search_query={search_query}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}'
            response = requests.get(api_url, timeout=20)
            response.raise_for_status()
            xml_data = response.content
            namespace = {'atom': 'http://www.w3.org/2005/Atom'}
            root = ET.fromstring(xml_data)
            entries = root.findall('atom:entry', namespace)
            if not entries:
                logging.warning(f"Could not find any entries for category {category}.")
                continue
            category_sent_count = 0
            for entry in entries:
                title = entry.find('atom:title', namespace).text.strip()
                url = entry.find('atom:id', namespace).text.strip()
                title = ' '.join(title.split())
                source_name = f"arXiv {category}"
                message = {"source": source_name, "url": url, "title": title}
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                category_sent_count += 1
            logging.info(f"Successfully sent {category_sent_count} URLs from {source_name} to the queue.")
            total_sent_count += category_sent_count
        logging.info(f"Total URLs sent from ArXiv: {total_sent_count}")
    except Exception as e:
        logging.error(f"--- FATAL ERROR in ArXivCollector ---")
        logging.error(traceback.format_exc())
        raise

# TechCrunchCollectorとAINewsCollectorは不要なため削除

# ===================================================================
# Function 2: Article Summarizer
# ===================================================================
@app.queue_trigger(arg_name="msg", queue_name="urls-to-summarize",
                   connection="MyStorageQueueConnectionString")
def ArticleSummarizer(msg: func.QueueMessage) -> None:
    logging.info(f"--- ArticleSummarizer INVOKED. MessageId: {msg.id} ---")
    
    cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
    cosmos_key = os.environ.get('COSMOS_KEY')
    db_client = CosmosClient(cosmos_endpoint, credential=cosmos_key).get_database_client(os.environ['COSMOS_DATABASE_NAME'])
    articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

    message = None
    item_id = None
    url = None
    
    try:
        message = json.loads(msg.get_body().decode('utf-8'))
        url = message.get("url")
        if not url:
            logging.error("URL is missing in the queue message. Skipping.")
            return

        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
        original_title = message.get("title", "No Title")
        source = message.get("source", "Unknown")
        rank = message.get("rank")

        try:
            # DBにアイテムが存在するかを確認
            articles_container.read_item(item=item_id, partition_key=item_id)
            # アイテムが存在した場合、ステータスに関わらず処理をスキップする
            logging.info(f"Article already exists in DB. Skipping processing. URL: {url}")
            return
        except exceptions.CosmosResourceNotFoundError:
            # DBにアイテムが存在しない（新しい記事の）場合のみ、処理を続行
            logging.info(f"New article. Proceeding with processing. URL: {url}")
            pass
        
        article_text = ""
        try:
            logging.info(f"Attempting to fetch content from: {url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
            response = requests.get(url, headers=headers, timeout=15, verify=False)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'lxml')
            for element in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
                element.decompose()
            article_text = ' '.join(t.strip() for t in soup.stripped_strings)

        except requests.exceptions.RequestException as re:
            logging.warning(f"Failed to fetch content from {url} due to RequestException: {re}. Will attempt title translation only.")
            article_text = ""

        if not article_text:
            logging.warning(f"Article body is empty for {url}. Proceeding with title translation only.")
            translated_title = _get_title_translation_from_azure_openai(original_title)
            
            _upsert_metadata_to_cosmos(
                item_id=item_id, url=url, title=translated_title, source=source,
                blob_name=None, original_title=original_title, rank=rank, status='title_only'
            )
            logging.info(f"Successfully processed title only for: {url}")

        else:
            translated_title, summary = _get_summary_and_title_from_azure_openai(article_text, original_title)
            blob_name = _save_summary_to_blob(summary, translated_title, url)
            
            _upsert_metadata_to_cosmos(
                item_id=item_id, url=url, title=translated_title, source=source,
                blob_name=blob_name, original_title=original_title, rank=rank, status='summarized'
            )
            logging.info(f"Successfully processed and summarized: {translated_title}")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in ArticleSummarizer for URL: {url} ---")
        logging.error(traceback.format_exc())
        
        if message and item_id:
            _upsert_metadata_to_cosmos(
                item_id=item_id, url=url, title=message.get("title", "No Title"), 
                source=message.get("source", "Unknown"), blob_name=None, 
                original_title=message.get("title", "No Title"), rank=message.get("rank"), status='failed'
            )
        raise e
    finally:
        time.sleep(1)

# ===================================================================
# Helper Functions
# ===================================================================
def _get_title_translation_from_azure_openai(title: str) -> str:
    azure_openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_openai_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_openai_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
    if not all([azure_openai_endpoint, azure_openai_key, azure_openai_deployment]):
        raise ValueError("Azure OpenAIの接続設定が不完全です。")
    client = AzureOpenAI(
        azure_endpoint=azure_openai_endpoint,
        api_key=azure_openai_key,
        api_version="2024-02-01",
        max_retries=2
    )
    system_prompt = """あなたは、与えられた英語のタイトルを日本語に翻訳する優秀なAIアシスタントです。ユーザーからの入力に対し、必ず以下のJSON形式で回答してください:
{"translated_title": "翻訳された日本語のタイトル"}"""
    user_prompt = f"以下のタイトルを日本語に翻訳してください。\n\n# 元のタイトル\n{title}"
    try:
        completion = client.chat.completions.create(
            model=azure_openai_deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        response_json = json.loads(completion.choices[0].message.content)
        translated_title = response_json.get("translated_title")
        if not translated_title:
            raise ValueError(f"OpenAI response is missing 'translated_title' key. Response: {response_json}")
        return translated_title
    except Exception as e:
        logging.error(f"Azure OpenAI API call for title translation failed: {e}")
        raise e

def _get_summary_and_title_from_azure_openai(text: str, title: str) -> tuple[str, str]:
    azure_openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_openai_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_openai_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
    if not all([azure_openai_endpoint, azure_openai_key, azure_openai_deployment]):
        raise ValueError("Azure OpenAIの接続設定が不完全です。")
    client = AzureOpenAI(
        azure_endpoint=azure_openai_endpoint,
        api_key=azure_openai_key,
        api_version="2024-02-01",
        max_retries=3
    )
    system_prompt = """あなたは、技術記事を要約し、そのタイトルを日本語に翻訳する優秀なAIアシスタントです。ユーザーからの入力に対し、必ず以下のJSON形式で回答してください:
{"translated_title": "翻訳された日本語のタイトル", "summary": "300字程度の日本語の要約"}"""
    user_prompt = f"以下の記事のタイトルを日本語に翻訳し、本文を日本語で要約してください。\n\n# 元のタイトル\n{title}\n\n# 記事の本文\n{text[:4000]}"
    try:
        completion = client.chat.completions.create(
            model=azure_openai_deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        response_json = json.loads(completion.choices[0].message.content)
        translated_title = response_json.get("translated_title")
        summary = response_json.get("summary")

        if not translated_title or not summary:
            raise ValueError(f"OpenAI response is missing 'translated_title' or 'summary' key. Response: {response_json}")

        return translated_title, summary
    except Exception as e:
        logging.error(f"Azure OpenAI API call failed with exception: {e}")
        raise e

def _save_summary_to_blob(summary: str, title: str, url: str) -> str:
    storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
    if not storage_connection_string:
        raise ValueError("ストレージの接続文字列が設定されていません。")
    blob_service_client = BlobServiceClient.from_connection_string(storage_connection_string)
    container_name = os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries")
    summary_container_client = blob_service_client.get_container_client(container_name)
    try:
        summary_container_client.create_container()
    except ResourceExistsError:
        pass
    blob_name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}/{uuid.uuid4()}.md"
    md_content = summary
    blob_client = summary_container_client.get_blob_client(blob_name)
    blob_client.upload_blob(md_content.encode('utf-8'), overwrite=True)
    logging.info(f"Summary saved to blob: {blob_name}")
    return blob_name

def _upsert_metadata_to_cosmos(item_id: str, url: str, title: str, source: str, blob_name: str | None, original_title: str, status: str, rank: int = None):
    cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
    cosmos_key = os.environ.get('COSMOS_KEY')
    if not cosmos_endpoint or not cosmos_key:
        raise ValueError("Cosmos DBの接続設定が不完全です。")
    cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
    db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
    articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])
    item_body = {
        'id': item_id, 'source': source, 'url': url, 'title': title,
        'original_title': original_title, 'summary_blob_path': blob_name,
        'processed_at': datetime.now(timezone.utc).isoformat(), 'status': status
    }
    if blob_name is None:
        item_body['summary_blob_path'] = None
    if rank is not None:
        item_body['rank'] = rank
    articles_container.upsert_item(body=item_body)
    logging.info(f"Metadata upserted to Cosmos DB with id: {item_id}, status: {status}")

# ===================================================================
# Web UI (FastAPI)
# ===================================================================
fast_app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.route(route="{*path}", auth_level=func.AuthLevel.ANONYMOUS, methods=["get", "post", "put", "delete"])
def WebUI(req: func.HttpRequest) -> func.HttpResponse:
    return func.AsgiMiddleware(fast_app).handle(req)

@fast_app.get("/api/articles_data", response_model=list)
async def get_all_articles_data():
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        if not cosmos_endpoint or not cosmos_key:
            raise HTTPException(status_code=500, detail="Cosmos DBの接続設定が不完全です。")

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

        all_items = []
        categories = ['HackerNews', 'arXiv cs.AI', 'arXiv cs.LG']
        
        for category in categories:
            if category == 'HackerNews':
                query = f"SELECT * FROM c WHERE c.source = '{category}' ORDER BY c.rank ASC OFFSET 0 LIMIT 100"
            else:
                query = f"SELECT * FROM c WHERE c.source = '{category}' ORDER BY c.processed_at DESC OFFSET 0 LIMIT 100"
            
            items = list(articles_container.query_items(
                query=query, enable_cross_partition_query=True
            ))
            all_items.extend(items)
        
        all_items.sort(key=lambda x: x.get('processed_at', ''), reverse=True)
        return all_items
    except Exception as e:
        logging.error(f"Error fetching all articles data: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="記事データの取得中にエラーが発生しました。")

@fast_app.get("/api/", response_class=HTMLResponse)
@fast_app.get("/api/front", response_class=HTMLResponse)
async def read_root(request: Request):
    try:
        return templates.TemplateResponse("index.html", {"request": request, "articles": []})
    except Exception as e:
        logging.error(f"Error reading root HTML page: {e}\n{traceback.format_exc()}")
        return HTMLResponse("記事一覧の取得中にエラーが発生しました。", status_code=500)

@fast_app.get("/article/{article_id}", response_class=HTMLResponse)
async def read_single_article_page(request: Request, article_id: str):
    logging.info(f"Permalink request for article ID: {article_id}")
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        storage_conn_str = os.environ.get("MyStorageQueueConnectionString")
        if not all([cosmos_endpoint, cosmos_key, storage_conn_str]):
             raise HTTPException(status_code=500, detail="接続設定が不完全です。")
        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])
        query = f"SELECT * FROM c WHERE c.id = '{article_id}'"
        items = list(articles_container.query_items(query=query, enable_cross_partition_query=True))
        if not items:
            raise HTTPException(status_code=404, detail="指定された記事が見つかりません。")
        article_meta = items[0]
        
        if article_meta.get('status') == 'title_only':
            html_content = markdown.markdown("この記事の本文は取得できませんでした。")
        elif article_meta.get('status') == 'failed' or not article_meta.get('summary_blob_path'):
            html_content = markdown.markdown("要約の生成中にエラーが発生しました。")
        else:
            blob_service_client = BlobServiceClient.from_connection_string(storage_conn_str)
            blob_client = blob_service_client.get_blob_client(
                container=os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries"),
                blob=article_meta['summary_blob_path']
            )
            if not blob_client.exists():
                raise HTTPException(status_code=404, detail="要約ファイルが見つかりません。")
            markdown_content = blob_client.download_blob().readall().decode('utf-8')
            html_content = markdown.markdown(markdown_content)

        return templates.TemplateResponse("permalink.html", {
            "request": request, "title": article_meta.get('title', 'No Title'),
            "content": html_content, "source_url": article_meta.get('url', '#'),
            "original_title": article_meta.get('original_title', ''),
            "source": article_meta.get('source', 'Unknown')
        })
    except Exception as e:
        logging.error(f"Error reading permalink article {article_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="記事の表示中にエラーが発生しました。")

@fast_app.get("/api/article/{article_id}", response_class=HTMLResponse)
async def read_article(request: Request, article_id: str):
    logging.info(f"Web UI: Reading article with ID: {article_id}")
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        storage_conn_str = os.environ.get("MyStorageQueueConnectionString")
        if not all([cosmos_endpoint, cosmos_key, storage_conn_str]):
             raise HTTPException(status_code=500, detail="接続設定が不完全です。")
        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])
        query = f"SELECT * FROM c WHERE c.id = '{article_id}'"
        items = list(articles_container.query_items(query=query, enable_cross_partition_query=True))
        if not items:
            raise HTTPException(status_code=404, detail="指定された記事が見つかりません。")
        article_meta = items[0]

        if article_meta.get('status') == 'title_only':
            html_content = markdown.markdown("この記事の本文は取得できませんでした。")
        elif article_meta.get('status') == 'failed' or not article_meta.get('summary_blob_path'):
            html_content = markdown.markdown("要約の生成中にエラーが発生しました。")
        else:
            blob_service_client = BlobServiceClient.from_connection_string(storage_conn_str)
            blob_client = blob_service_client.get_blob_client(
                container=os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries"),
                blob=article_meta['summary_blob_path']
            )
            if not blob_client.exists():
                raise HTTPException(status_code=404, detail="要約ファイルが見つかりません。")
            markdown_content = blob_client.download_blob().readall().decode('utf-8')
            html_content = markdown.markdown(markdown_content)

        return templates.TemplateResponse("article.html", {
            "request": request, "id": article_id, "title": article_meta.get('title', 'No Title'),
            "content": html_content, "source_url": article_meta.get('url', '#'),
            "original_title": article_meta.get('original_title', ''),
            "source": article_meta.get('source', 'Unknown')
        })
    except Exception as e:
        logging.error(f"Error reading article {article_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="記事の表示中にエラーが発生しました。")
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
import azure.functions as func

from fastapi.responses import Response
from email.utils import formatdate
from urllib.parse import urljoin

from azure.cosmos import CosmosClient, exceptions
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient
from bs4 import BeautifulSoup
from openai import AzureOpenAI
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = func.FunctionApp()

# ===================================================================
# データ収集関数群 (変更なし)
# ===================================================================
@app.schedule(schedule="0 */6 * * *", arg_name="myTimer", run_on_startup=False)
def HackerNewsCollector(myTimer: func.TimerRequest) -> None:
    logging.info('Hacker News Collector function ran.')
    try:
        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set.")
        HACKER_NEWS_API_BASE = "https://hacker-news.firebaseio.com/v0"
        TARGET_STORIES = 100
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
        for story_id in story_ids:
            story_detail_url = f"{HACKER_NEWS_API_BASE}/item/{story_id}.json"
            story_res = requests.get(story_detail_url, timeout=15)
            story_data = story_res.json()
            if story_data and "url" in story_data:
                message = {
                    "source": "HackerNews",
                    "url": story_data["url"],
                    "title": story_data.get("title", "No Title")
                }
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                sent_count += 1
        logging.info(f"Successfully sent {sent_count} URLs to the queue.")
    except Exception as e:
        logging.error(f"--- FATAL ERROR in HackerNewsCollector ---")
        logging.error(traceback.format_exc())
        raise

@app.schedule(schedule="5 */6 * * *", arg_name="myTimer", run_on_startup=False)
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
        max_results = 100
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
                source_name_for_message = f"arXiv {category}"
                message = {"source": source_name_for_message, "url": url, "title": title}
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                category_sent_count += 1
            logging.info(f"Successfully sent {category_sent_count} URLs from arXiv {category} to the queue.")
            total_sent_count += category_sent_count
        logging.info(f"Total URLs sent from ArXiv: {total_sent_count}")
    except Exception as e:
        logging.error(f"--- FATAL ERROR in ArXivCollector ---")
        logging.error(traceback.format_exc())
        raise

@app.schedule(schedule="10 */6 * * *", arg_name="myTimer", run_on_startup=False)
def TechCrunchCollector(myTimer: func.TimerRequest) -> None:
    logging.info('TechCrunch Collector function ran.')
    try:
        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set.")

        TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"

        response = requests.get(TECHCRUNCH_RSS_URL, timeout=15)
        response.raise_for_status()

        xml_data = response.content
        root = ET.fromstring(xml_data)

        queue_client = QueueClient.from_connection_string(
            conn_str=storage_connection_string,
            queue_name=os.environ.get("QUEUE_NAME", "urls-to-summarize")
        )

        sent_count = 0
        for item in root.findall('.//channel/item'):
            title = item.find('title').text
            url = item.find('link').text

            if title and url:
                message = {
                    "source": "TechCrunch",
                    "url": url,
                    "title": title
                }
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                sent_count += 1

        logging.info(f"Successfully sent {sent_count} URLs from TechCrunch to the queue.")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in TechCrunchCollector ---")
        logging.error(traceback.format_exc())
        raise

@app.schedule(schedule="15 */6 * * *", arg_name="myTimer", run_on_startup=False)
def HuggingFaceBlogCollector(myTimer: func.TimerRequest) -> None:
    logging.info('Hugging Face Blog Collector function ran.')
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        if not cosmos_endpoint or not cosmos_key:
            raise ValueError("Cosmos DBの接続設定が不完全です。")
        
        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set.")
        
        HF_BLOG_RSS_URL = "https://huggingface.co/blog/feed.xml"
        
        response = requests.get(HF_BLOG_RSS_URL, timeout=15)
        response.raise_for_status()

        xml_data = response.content
        root = ET.fromstring(xml_data)
        
        queue_client = QueueClient.from_connection_string(
            conn_str=storage_connection_string,
            queue_name=os.environ.get("QUEUE_NAME", "urls-to-summarize")
        )

        sent_count = 0
        # RSSフィードは新しい順に並んでいると仮定
        for item in root.findall('.//channel/item'):
            title = item.find('title').text
            url = item.find('link').text
            
            if not (title and url):
                continue

            item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
            
            try:
                # データベースに同じ記事が既に存在するか確認
                articles_container.read_item(item=item_id, partition_key=item_id)
                # 存在した場合、これ以上古い記事をチェックする必要はないのでループを抜ける
                logging.info(f"Found existing article, stopping collection for this run. URL: {url}")
                break 
            except exceptions.CosmosResourceNotFoundError:
                # 存在しない場合は新しい記事なので、キューに追加
                message = {
                    "source": "HuggingFace",
                    "url": url, 
                    "title": title
                }
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                sent_count += 1
        
        logging.info(f"Finished Hugging Face Blog collection. Sent {sent_count} new URLs to the queue.")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in HuggingFaceBlogCollector ---")
        logging.error(traceback.format_exc())
        raise

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
        
        # ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
        # ★★★ 取得済み記事の日時上書きを確実に防止する修正 ★★★
        # ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
        try:
            # 記事IDをパーティションキーとしてアイテムを検索 (最も確実な方法)
            articles_container.read_item(item=item_id, partition_key=item_id)
            logging.info(f"Article already exists in DB. Skipping processing. URL: {url}")
            return
        except exceptions.CosmosResourceNotFoundError:
            logging.info(f"New article. Proceeding with processing. URL: {url}")
            pass
        
        original_title = message.get("title", "No Title")
        source = message.get("source", "Unknown")

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
                blob_name=None, original_title=original_title, status='title_only'
            )
            logging.info(f"Successfully processed title only for: {url}")

        else:
            translated_title, summary = _get_summary_and_title_from_azure_openai(article_text, original_title)
            blob_name = _save_summary_to_blob(summary, translated_title, url)

            _upsert_metadata_to_cosmos(
                item_id=item_id, url=url, title=translated_title, source=source,
                blob_name=blob_name, original_title=original_title, status='summarized'
            )
            logging.info(f"Successfully processed and summarized: {translated_title}")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in ArticleSummarizer for URL: {url} ---")
        logging.error(traceback.format_exc())

        if message and item_id:
            _upsert_metadata_to_cosmos(
                item_id=item_id, url=url, title=message.get("title", "No Title"),
                source=message.get("source", "Unknown"), blob_name=None,
                original_title=message.get("title", "No Title"), status='failed'
            )
        raise e
    finally:
        time.sleep(1)

# ===================================================================
# Helper Functions
# ===================================================================
def _get_title_translation_from_azure_openai(title: str) -> str:
    # (この関数は変更なし)
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
    # (この関数は変更なし)
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
    # (この関数は変更なし)
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

def _upsert_metadata_to_cosmos(item_id: str, url: str, title: str, source: str, blob_name: str | None, original_title: str, status: str):
    # (この関数は変更なし)
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
    articles_container.upsert_item(body=item_body)
    logging.info(f"Metadata upserted to Cosmos DB with id: {item_id}, status: {status}")

# ===================================================================
# Web UI (FastAPI)
# ===================================================================
fast_app = FastAPI()
templates = Jinja2Templates(directory="templates")
fast_app.mount("/static", StaticFiles(directory="static"), name="static")

@app.route(route="{*path}", auth_level=func.AuthLevel.ANONYMOUS, methods=["get", "post", "put", "delete"])
def WebUI(req: func.HttpRequest) -> func.HttpResponse:
    return func.AsgiMiddleware(fast_app).handle(req)

# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★★★ arXivフィルターの不具合を修正した最終確定版のコード ★★★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
@fast_app.get("/api/articles_data", response_model=list)
async def get_all_articles_data(source: str = 'All', skip: int = 0, limit: int = 50):
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        if not cosmos_endpoint or not cosmos_key:
            raise HTTPException(status_code=500, detail="Cosmos DBの接続設定が不完全です。")

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

        items = []
        if source == 'arXiv':
            # 確実性を優先し、2回クエリを実行して結果を結合・ソートする方法
            query_ai = "SELECT * FROM c WHERE c.source = 'arXiv cs.AI'"
            items_ai = list(articles_container.query_items(query=query_ai, enable_cross_partition_query=True))
            
            query_lg = "SELECT * FROM c WHERE c.source = 'arXiv cs.LG'"
            items_lg = list(articles_container.query_items(query=query_lg, enable_cross_partition_query=True))
            
            all_items = items_ai + items_lg
            all_items.sort(key=lambda x: x['processed_at'], reverse=True)
            
            items = all_items[skip : skip + limit]
        else:
            # All または HackerNews, TechCrunch, HuggingFace の場合
            parameters = []
            query = "SELECT * FROM c"
            if source != 'All':
                query += " WHERE c.source = @source"
                parameters.append({"name": "@source", "value": source})

            query += f" ORDER BY c.processed_at DESC OFFSET {skip} LIMIT {limit}"

            items = list(articles_container.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True
            ))

        return items
    except Exception as e:
        logging.error(f"Error fetching articles data: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="記事データの取得中にエラーが発生しました。")

# (以降のコードは変更なし)
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

        context = {
            "request": request, # この行を追加
            "title": article_meta.get('title', 'No Title'),
            "content": html_content,
            "source_url": article_meta.get('url', '#'),
            "original_title": article_meta.get('original_title', ''),
            "source": article_meta.get('source', 'Unknown')
        }

        return templates.TemplateResponse("permalink.html", context)
    
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

@fast_app.get("/api/rss", response_class=Response)
async def generate_rss_feed(request: Request):
    logging.info("RSS feed request received.")
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        storage_conn_str = os.environ.get("MyStorageQueueConnectionString")
        if not all([cosmos_endpoint, cosmos_key, storage_conn_str]):
            raise HTTPException(status_code=500, detail="RSS生成に必要な接続設定が不完全です。")

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])
        query = "SELECT * FROM c WHERE c.status = 'summarized' ORDER BY c.processed_at DESC OFFSET 0 LIMIT 50"
        items = list(articles_container.query_items(query=query, enable_cross_partition_query=True))

        blob_service_client = BlobServiceClient.from_connection_string(storage_conn_str)
        summary_container_name = os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries")

        rss = ET.Element("rss", version="2.0", attrib={"xmlns:atom": "http://www.w3.org/2005/Atom"})
        channel = ET.SubElement(rss, "channel")

        site_title = "Tech News Summarizer"
        base_url = str(request.base_url)
        site_link = urljoin(base_url, "api/front")

        ET.SubElement(channel, "title").text = site_title
        ET.SubElement(channel, "link").text = site_link
        ET.SubElement(channel, "description").text = "Hacker NewsやarXivなどの最新技術記事の要約"
        ET.SubElement(channel, "language").text = "ja"
        ET.SubElement(channel, "lastBuildDate").text = formatdate(datetime.now(timezone.utc).timestamp(), usegmt=True)
        ET.SubElement(channel, "atom:link", href=urljoin(base_url, "api/rss"), rel="self", type="application/rss+xml")

        for item in items:
            item_elem = ET.SubElement(channel, "item")

            source_text = item.get('source', 'Unknown')
            original_title = item.get('title', 'No Title')
            ET.SubElement(item_elem, "title").text = f"[{source_text}] {original_title}"
            ET.SubElement(item_elem, "category").text = source_text

            article_link = urljoin(base_url, f"article/{item['id']}")
            ET.SubElement(item_elem, "link").text = article_link
            ET.SubElement(item_elem, "guid", isPermaLink="false").text = item['id']

            pub_date_str = item.get('processed_at')
            if pub_date_str:
                dt_object = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00'))
                ET.SubElement(item_elem, "pubDate").text = formatdate(dt_object.timestamp(), usegmt=True)

            description = "要約を読み込めませんでした。"
            blob_path = item.get('summary_blob_path')
            if blob_path:
                try:
                    blob_client = blob_service_client.get_blob_client(container=summary_container_name, blob=blob_path)
                    if blob_client.exists():
                        description = blob_client.download_blob().readall().decode('utf-8')
                    else:
                        logging.warning(f"Summary blob not found for RSS: {blob_path}")
                        description = "要約ファイルが見つかりませんでした。"
                except Exception as e:
                    logging.error(f"RSS Feed Gen: Failed to read summary blob {blob_path}: {e}")
                    description = "要約の読み込み中にエラーが発生しました。"

            ET.SubElement(item_elem, "description").text = description

        rss_string = ET.tostring(rss, encoding='UTF-8', method='xml', xml_declaration=True).decode('utf-8')
        return Response(content=rss_string, media_type="application/xml; charset=utf-8")

    except Exception as e:
        logging.error(f"Error generating RSS feed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"RSSフィードの生成中にエラーが発生しました: {e}")
    
@fast_app.get("/sitemap.xml", response_class=Response)
async def generate_sitemap(request: Request):
    logging.info("Sitemap generation request received.")
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        if not cosmos_endpoint or not cosmos_key:
            raise HTTPException(status_code=500, detail="Sitemapに必要な接続設定が不完全です。")

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])
        
        query = "SELECT c.id, c.processed_at FROM c WHERE c.status = 'summarized'"
        items = list(articles_container.query_items(query=query, enable_cross_partition_query=True))

        urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

        base_url = str(request.base_url)

        for item in items:
            url_element = ET.SubElement(urlset, "url")
            
            loc = ET.SubElement(url_element, "loc")
            loc.text = urljoin(base_url, f"article/{item['id']}")
            
            lastmod = ET.SubElement(url_element, "lastmod")
            lastmod.text = datetime.fromisoformat(item['processed_at'].replace('Z', '+00:00')).strftime('%Y-%m-%d')
            
            ET.SubElement(url_element, "changefreq").text = "daily"
            ET.SubElement(url_element, "priority").text = "0.8"

        sitemap_string = ET.tostring(urlset, encoding='UTF-8', method='xml', xml_declaration=True).decode('utf-8')
        return Response(content=sitemap_string, media_type="application/xml; charset=utf-8")

    except Exception as e:
        logging.error(f"Error generating sitemap: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Sitemapの生成中にエラーが発生しました: {e}")

@fast_app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def redirect_root_to_front():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/api/front")

@fast_app.get("/googlec43f505db609c105.html", response_class=FileResponse, include_in_schema=False)
async def read_google_verification():
    return "static/googlec43f505db609c105.html"

@fast_app.get("/robots.txt", response_class=FileResponse, include_in_schema=False)
async def read_robots_txt():
    return "static/robots.txt"
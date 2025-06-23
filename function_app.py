import azure.functions as func
import logging
import os
import json
import requests
import uuid
from datetime import datetime, timezone
import traceback
import time

# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# 以下のライブラリが requirements.txt に含まれていることを確認してください
# azure-functions, requests, azure-storage-queue, azure-storage-blob,
# azure-cosmos, beautifulsoup4, lxml, openai
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★

from azure.cosmos import CosmosClient
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient
from bs4 import BeautifulSoup
from openai import AzureOpenAI

app = func.FunctionApp()

# ===================================================================
# Function 1: Hacker News Collector (タイマートリガー)
# ===================================================================
@app.schedule(schedule="0 0 10 * * *", arg_name="myTimer", run_on_startup=False)
def HackerNewsCollector(myTimer: func.TimerRequest) -> None:
    logging.info('Hacker News Collector function ran.')
    try:
        storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
        if not storage_connection_string:
            raise ValueError("MyStorageQueueConnectionString is not set in local.settings.json")

        HACKER_NEWS_API_BASE = "https://hacker-news.firebaseio.com/v0"
        TARGET_STORIES = 30

        top_stories_url = f"{HACKER_NEWS_API_BASE}/topstories.json"
        response = requests.get(top_stories_url, timeout=15)
        response.raise_for_status()
        story_ids = response.json()[:TARGET_STORIES]
        logging.info(f"Successfully fetched {len(story_ids)} story IDs.")

        queue_client = QueueClient.from_connection_string(
            conn_str=storage_connection_string,
            queue_name=os.environ.get("QUEUE_NAME", "urls-to-summarize")
        )
        try:
            queue_client.create_queue()
        except Exception:
            pass

        sent_count = 0
        for story_id in story_ids:
            story_detail_url = f"{HACKER_NEWS_API_BASE}/item/{story_id}.json"
            story_res = requests.get(story_detail_url, timeout=15)
            story_data = story_res.json()
            if story_data and "url" in story_data:
                # 元のタイトルをそのままキューに入れる
                message = {"source": "HackerNews", "url": story_data["url"], "title": story_data.get("title", "No Title")}
                queue_client.send_message(json.dumps(message, ensure_ascii=False))
                sent_count += 1
        logging.info(f"Successfully sent {sent_count} URLs to the queue.")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in HackerNewsCollector ---")
        logging.error(traceback.format_exc())
        raise

# ===================================================================
# Function 2: Article Summarizer (キュー・トリガー)
# ===================================================================
@app.queue_trigger(arg_name="msg", queue_name="urls-to-summarize",
                   connection="MyStorageQueueConnectionString")
def ArticleSummarizer(msg: func.QueueMessage) -> None:
    logging.info(f"--- ArticleSummarizer INVOKED. MessageId: {msg.id} ---")

    try:
        # STEP 1: メッセージの解析
        message = json.loads(msg.get_body().decode('utf-8'))
        url = message.get("url")
        original_title = message.get("title", "No Title") # 元のタイトル
        source = message.get("source", "Unknown")

        if not url:
            logging.error("URL is missing in the queue message. Skipping.")
            return

        logging.info(f"Processing article: {original_title} ({url})")

        # STEP 2: 記事本文の取得
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        for element in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            element.decompose()
        article_text = ' '.join(t.strip() for t in soup.stripped_strings)

        if not article_text:
            raise ValueError("Failed to extract text from URL.")

        # STEP 3: Azure OpenAIによる要約とタイトル翻訳
        # _get_summary_and_title_from_azure_openai は要約文と翻訳されたタイトルを両方返す
        translated_title, summary = _get_summary_and_title_from_azure_openai(article_text, original_title)

        # もし翻訳に失敗した場合は、元のタイトルをそのまま使う
        if not translated_title:
            translated_title = original_title
            logging.warning("Title translation failed. Using original title.")

        # STEP 4: Blobへの保存
        # 保存するMarkdownには翻訳されたタイトルを使用
        blob_name = _save_summary_to_blob(summary, translated_title, url)

        # STEP 5: Cosmos DBへの保存
        # Cosmos DBにも翻訳されたタイトルを保存
        _upsert_metadata_to_cosmos(url, translated_title, source, blob_name)

        logging.info(f" Successfully processed and summarized: {translated_title} ")

    except Exception as e:
        logging.error(f"--- FATAL ERROR in ArticleSummarizer ---")
        logging.error(f"Message Body: {msg.get_body().decode('utf-8')}")
        logging.error(traceback.format_exc())
        raise e

    finally:
        logging.info("Waiting for 4 seconds before processing next message to respect API rate limits...")
        time.sleep(4)


# ===================================================================
# ヘルパー関数
# ===================================================================

def _get_summary_and_title_from_azure_openai(text: str, title: str) -> tuple[str, str]:
    azure_openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_openai_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_openai_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")

    if not all([azure_openai_endpoint, azure_openai_key, azure_openai_deployment]):
        raise ValueError("Azure OpenAIの接続設定が不完全です。")

    client = AzureOpenAI(
        azure_endpoint=azure_openai_endpoint,
        api_key=azure_openai_key,
        api_version="2024-02-01"
    )

    system_prompt = "あなたは、技術記事を要約し、そのタイトルを日本語に翻訳する優秀なAIアシスタントです。ユーザーからの入力に対し、必ず以下のJSON形式で回答してください:\n{\"translated_title\": \"翻訳された日本語のタイトル\", \"summary\": \"300字程度の日本語の要約\"}"

    user_prompt = f"""以下の記事のタイトルを日本語に翻訳し、本文を日本語で要約してください。

# 元のタイトル
{title}

# 記事の本文
{text[:8000]}
"""

    completion = client.chat.completions.create(
        model=azure_openai_deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"} # 回答形式をJSONに指定
    )

    try:
        response_json = json.loads(completion.choices[0].message.content)
        translated_title = response_json.get("translated_title", title)
        summary = response_json.get("summary", "要約の生成に失敗しました。")
        return translated_title, summary
    except (json.JSONDecodeError, AttributeError):
        logging.error("Failed to decode JSON response from AI. Falling back to summary only.")
        # JSONの解析に失敗した場合、要約のみを試みるか、エラーとして扱う
        return title, completion.choices[0].message.content


def _save_summary_to_blob(summary: str, title: str, url: str) -> str:
    storage_connection_string = os.environ.get("MyStorageQueueConnectionString")
    if not storage_connection_string:
        raise ValueError("ストレージの接続文字列が設定されていません。")

    blob_service_client = BlobServiceClient.from_connection_string(storage_connection_string)
    container_name = os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries")
    summary_container_client = blob_service_client.get_container_client(container_name)

    try:
        summary_container_client.create_container()
    except Exception:
        pass

    blob_name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}/{uuid.uuid4()}.md"
    md_content = f"# {title}\n\n**Source:** [{url}]({url})\n\n---\n\n{summary}"
    blob_client = summary_container_client.get_blob_client(blob_name)
    blob_client.upload_blob(md_content.encode('utf-8'), overwrite=True)
    logging.info(f"Summary saved to blob: {blob_name}")
    return blob_name

def _upsert_metadata_to_cosmos(url: str, title: str, source: str, blob_name: str):
    cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
    cosmos_key = os.environ.get('COSMOS_KEY')
    if not cosmos_endpoint or not cosmos_key:
        raise ValueError("Cosmos DBの接続設定が不完全です。")

    cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
    db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
    articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    item_body = {
        'id': item_id,
        'source': source,
        'url': url,
        'title': title, # ここには翻訳済みのタイトルが入る
        'summary_blob_path': blob_name,
        'processed_at': datetime.now(timezone.utc).isoformat(),
        'status': 'summarized'
    }
    articles_container.upsert_item(body=item_body)
    logging.info(f"Metadata upserted to Cosmos DB with id: {item_id}")

# === ここからWeb UI用のコードを追記 ===

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import markdown

# FastAPIアプリケーションを初期化
fast_app = FastAPI()

# HTMLテンプレートの場所を指定
templates = Jinja2Templates(directory="templates")

# Web UI用のHTTPトリガー関数
# これがFastAPIアプリ全体をラップし、Azure FunctionsのHTTPトリガーとして機能します。
@app.route(route="{*path}", auth_level=func.AuthLevel.ANONYMOUS, methods=["get", "post"])
def WebUI(req: func.HttpRequest) -> func.HttpResponse:
    """
    This function handles all requests to the Web UI.
    It uses the asgi_http adapter to convert the Azure Functions request
    into an ASGI request that FastAPI can understand.
    """
    return func.AsgiMiddleware(fast_app).handle(req)


@fast_app.get("/api/", response_class=HTMLResponse) # 変更点
@fast_app.get("/api/front", response_class=HTMLResponse) # 変更点
async def read_root(request: Request):
    """
    トップページ（記事一覧）を表示します。
    Cosmos DBから最新の20件の記事を取得して表示します。
    """
    logging.info("Web UI: Reading root page.")
    try:
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        if not cosmos_endpoint or not cosmos_key:
            return HTMLResponse("Cosmos DBの接続設定が不完全です。", status_code=500)

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

        # processed_atで降順にソートし、最新20件を取得
        query = "SELECT * FROM c ORDER BY c.processed_at DESC OFFSET 0 LIMIT 20"

        items = list(articles_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))

        return templates.TemplateResponse("index.html", {"request": request, "articles": items})
    except Exception as e:
        logging.error(f"Error reading root page: {e}\n{traceback.format_exc()}")
        return HTMLResponse("記事一覧の取得中にエラーが発生しました。", status_code=500)


@fast_app.get("/api/article/{article_id}", response_class=HTMLResponse) # 変更点
async def read_article(request: Request, article_id: str):
    """
    記事詳細ページを表示します。
    IDを元にCosmos DBからメタデータを取得し、Blob Storageから要約MDファイルを取得してHTMLに変換して表示します。
    """
    logging.info(f"Web UI: Reading article with ID: {article_id}")
    try:
        # Cosmos DBから記事メタデータを取得
        cosmos_endpoint = os.environ.get('COSMOS_ENDPOINT')
        cosmos_key = os.environ.get('COSMOS_KEY')
        storage_conn_str = os.environ.get("MyStorageQueueConnectionString")

        if not all([cosmos_endpoint, cosmos_key, storage_conn_str]):
             return HTMLResponse("接続設定が不完全です。", status_code=500)

        cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)
        db_client = cosmos_client.get_database_client(os.environ['COSMOS_DATABASE_NAME'])
        articles_container = db_client.get_container_client(os.environ['COSMOS_CONTAINER_NAME'])

        # パーティションキーを指定する必要がある場合
        # この例ではクロスパーティションクエリで代用
        query = f"SELECT * FROM c WHERE c.id = '{article_id}'"
        items = list(articles_container.query_items(query=query, enable_cross_partition_query=True))

        if not items:
            return HTMLResponse("指定された記事が見つかりません。", status_code=404)

        article_meta = items[0]

        # Blob StorageからMarkdownコンテンツを取得
        blob_service_client = BlobServiceClient.from_connection_string(storage_conn_str)
        blob_client = blob_service_client.get_blob_client(
            container=os.environ.get("SUMMARY_BLOB_CONTAINER_NAME", "summaries"),
            blob=article_meta['summary_blob_path']
        )

        markdown_content = blob_client.download_blob().readall().decode('utf-8')

        # MarkdownをHTMLに変換
        html_content = markdown.markdown(markdown_content)

        return templates.TemplateResponse("article.html", {"request": request, "title": article_meta['title'], "content": html_content})

    except Exception as e:
        logging.error(f"Error reading article {article_id}: {e}\n{traceback.format_exc()}")
        return HTMLResponse("記事の表示中にエラーが発生しました。", status_code=500)
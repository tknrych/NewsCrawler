# AI News Summarizer Dashboard

This project is a web application that automatically collects articles from multiple tech news sources like Hacker News and arXiv, summarizes them using the Azure OpenAI Service, and displays them on a filterable dashboard.

*A screenshot of the main dashboard.*

## Features

  - **Multi-Source Collection**: Fetches the latest articles from Hacker News (via API) and arXiv (`cs.AI`, `cs.LG` categories via API).
  - **AI-Powered Summarization**: Utilizes Azure OpenAI to generate concise summaries and translate titles into Japanese.
  - **Asynchronous Processing**: Uses Azure Queue Storage to decouple the article collection process from the summarization process, ensuring scalability and resilience.
  - **Modern Web UI**: A clean dashboard built with vanilla JavaScript that allows filtering articles by source category.
  - **Serverless Architecture**: Built entirely on Azure Functions, providing a cost-effective and scalable backend.

## System Architecture

The application operates based on an event-driven, serverless architecture:

1.  **Collector Functions (`HackerNewsCollector`, `ArXivCollector`)**: These timer-triggered functions run periodically (e.g., every hour). They fetch article metadata (title, URL) from the respective APIs and send a message for each article to an Azure Storage Queue.
2.  **Azure Queue Storage**: Acts as a message broker, holding the queue of articles that need to be summarized.
3.  **Summarizer Function (`ArticleSummarizer`)**: This function is triggered by new messages in the queue. For each message, it:
    a. Fetches the full content of the article from its URL.
    b. Calls the Azure OpenAI Service to get a Japanese summary and a translated title.
    c. Saves the generated summary as a text file in Azure Blob Storage.
    d. Saves the article's metadata (titles, URL, source, path to summary blob) to Azure Cosmos DB.
4.  **Web UI (FastAPI on Azure Functions)**: A FastAPI application running on an HTTP-triggered Azure Function serves the frontend dashboard. It provides an API endpoint (`/api/articles_data`) that retrieves the article list from Cosmos DB to be displayed on the UI.

## Technology Stack

  - **Backend**: Python, Azure Functions
  - **Web Framework**: FastAPI
  - **Database**: Azure Cosmos DB (for NoSQL)
  - **Storage**:
      - Azure Blob Storage (for summary text files)
      - Azure Queue Storage (for message queuing)
  - **AI Service**: Azure OpenAI Service
  - **Frontend**: HTML, CSS, JavaScript (Vanilla)

## Prerequisites

  - Python 3.9+
  - An active Azure Subscription
  - [Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
  - [Azure Functions Core Tools](https://docs.microsoft.com/en-us/azure/azure-functions/functions-run-local)

## Installation & Setup

1.  **Clone the repository:**

    ```bash
    git clone [your-repository-url]
    cd [repository-name]
    ```

2.  **Create and activate a virtual environment:**

    ```bash
    # For Windows
    python -m venv .venv
    .venv\Scripts\activate

    # For macOS/Linux
    python -m venv .venv
    source .venv/bin/activate
    ```

3.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up local settings:**

      - Create a file named `local.settings.json` in the root of the project.
      - This file stores your local environment variables and secrets. **It should not be committed to Git.**
      - Populate it with the necessary values as shown in the section below.

## Environment Variables

Your `local.settings.json` file should have the following structure:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "Your_Azure_Storage_Connection_String",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    
    "MyStorageQueueConnectionString": "Your_Azure_Storage_Connection_String",
    "QUEUE_NAME": "urls-to-summarize",
    "SUMMARY_BLOB_CONTAINER_NAME": "summaries",
    
    "COSMOS_ENDPOINT": "Your_Cosmos_DB_Endpoint",
    "COSMOS_KEY": "Your_Cosmos_DB_Primary_Key",
    "COSMOS_DATABASE_NAME": "NewsDB",
    "COSMOS_CONTAINER_NAME": "Articles",

    "AZURE_OPENAI_ENDPOINT": "Your_Azure_OpenAI_Endpoint",
    "AZURE_OPENAI_API_KEY": "Your_Azure_OpenAI_API_Key",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "Your_Deployment_Name"
  }
}
```

## Usage

To run the application locally, use the Azure Functions Core Tools:

```bash
func start
```

  - The FastAPI-based UI will be available at `http://localhost:7071/api/front`.
  - The timer-triggered functions will run based on their CRON schedule or on startup if configured.

## License

Distributed under theApache License 2.0 License. See `LICENSE` for more information.

-----

# AIニュース要約ダッシュボード

このプロジェクトは、Hacker NewsやarXivといった複数の技術ニュースソースから自動で記事を収集し、Azure OpenAI Serviceを利用して要約・翻訳を行い、フィルタリング可能なダッシュボードに表示するWebアプリケーションです。

*メインダッシュボードのスクリーンショット*

## 主な機能

  - **複数ソースからの収集**: Hacker News (API経由) および arXiv (`cs.AI`, `cs.LG`カテゴリ、API経由) から最新の記事を取得します。
  - **AIによる要約**: Azure OpenAIを利用して、簡潔な日本語の要約を生成し、タイトルを翻訳します。
  - **非同期処理**: Azure Queue Storageを利用して、記事の収集プロセスと要約プロセスを分離し、スケーラビリティと耐障害性を確保しています。
  - **モダンなWeb UI**: カテゴリーによる記事のフィルタリングが可能な、バニラJavaScriptで構築されたクリーンなダッシュボード。
  - **サーバーレスアーキテクチャ**: 全体をAzure Functions上で構築し、コスト効率と拡張性の高いバックエンドを実現しています。

## システムアーキテクチャ

本アプリケーションは、イベント駆動型のサーバーレスアーキテクチャに基づいています。

1.  **コレクター関数 (`HackerNewsCollector`, `ArXivCollector`)**: タイマートリガーで定期的（例: 1時間ごと）に実行されます。各APIから記事のメタデータ（タイトル、URL）を取得し、記事ごとにメッセージをAzure Storage Queueに送信します。
2.  **Azure Queue Storage**: メッセージブローカーとして機能し、要約が必要な記事のキューを保持します。
3.  **要約関数 (`ArticleSummarizer`)**: キューに新しいメッセージが追加されるとトリガーされます。各メッセージに対して以下の処理を行います。
    a. 記事のURLからコンテンツ全文を取得します。
    b. Azure OpenAI Serviceを呼び出し、日本語の要約と翻訳済みタイトルを取得します。
    c. 生成された要約をテキストファイルとしてAzure Blob Storageに保存します。
    d. 記事のメタデータ（タイトル、URL、ソース、要約Blobへのパスなど）をAzure Cosmos DBに保存します。
4.  **Web UI (FastAPI on Azure Functions)**: HTTPトリガーのAzure Function上で動作するFastAPIアプリケーションがフロントエンドのダッシュボードを提供します。UIに表示するための記事リストをCosmos DBから取得するAPIエンドポイント (`/api/articles_data`) を提供します。

## 技術スタック

  - **バックエンド**: Python, Azure Functions
  - **Webフレームワーク**: FastAPI
  - **データベース**: Azure Cosmos DB (NoSQL)
  - **ストレージ**:
      - Azure Blob Storage (要約テキストファイル用)
      - Azure Queue Storage (メッセージキュー用)
  - **AIサービス**: Azure OpenAI Service
  - **フロントエンド**: HTML, CSS, JavaScript (Vanilla)

## 事前準備

  - Python 3.9以上
  - 有効なAzureサブスクリプション
  - [Azure CLI](https://www.google.com/search?q=https://docs.microsoft.com/ja-jp/cli/azure/install-azure-cli)
  - [Azure Functions Core Tools](https://docs.microsoft.com/ja-jp/azure/azure-functions/functions-run-local)

## インストールとセットアップ

1.  **リポジトリをクローンします:**

    ```bash
    git clone [your-repository-url]
    cd [repository-name]
    ```

2.  **仮想環境を作成し、アクティベートします:**

    ```bash
    # Windowsの場合
    python -m venv .venv
    .venv\Scripts\activate

    # macOS/Linuxの場合
    python -m venv .venv
    source .venv/bin/activate
    ```

3.  **依存ライブラリをインストールします:**

    ```bash
    pip install -r requirements.txt
    ```

4.  **ローカル設定ファイルを準備します:**

      - プロジェクトのルートに `local.settings.json` という名前のファイルを作成します。
      - このファイルはローカル環境の環境変数やシークレット情報を格納します。**Gitにコミットしないでください。**
      - 以下のセクションを参考に、必要な値を設定します。

## 環境変数

`local.settings.json` ファイルは以下の構造を持つ必要があります。

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "使用するAzure Storageの接続文字列",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    
    "MyStorageQueueConnectionString": "使用するAzure Storageの接続文字列",
    "QUEUE_NAME": "urls-to-summarize",
    "SUMMARY_BLOB_CONTAINER_NAME": "summaries",
    
    "COSMOS_ENDPOINT": "Cosmos DBのエンドポイント",
    "COSMOS_KEY": "Cosmos DBの主キー",
    "COSMOS_DATABASE_NAME": "NewsDB",
    "COSMOS_CONTAINER_NAME": "Articles",

    "AZURE_OPENAI_ENDPOINT": "Azure OpenAIのエンドポイント",
    "AZURE_OPENAI_API_KEY": "Azure OpenAIのAPIキー",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "使用するデプロイ名"
  }
}
```

## 実行方法

ローカルでアプリケーションを実行するには、Azure Functions Core Toolsを使用します。

```bash
func start
```

  - FastAPIベースのUIは `http://localhost:7071/api/front` で利用可能になります。
  - タイマートリガー関数は、設定されたCRONスケジュール、または（設定に応じて）起動時に実行されます。

## ライセンス

このプロジェクトはApache License 2.0ライセンスの下で配布されています。詳細については `LICENSE` ファイルをご覧ください。
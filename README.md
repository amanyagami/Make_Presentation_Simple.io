# 🚀 Make Presentation Simple

Transform PDFs into structured presentation decks using a fully serverless, AI-powered pipeline.

🌐 **Live App:**
[https://vakt-artifactbucket-vxgd6cj5bqrc.s3.us-east-1.amazonaws.com/index_.html](https://vakt-artifactbucket-vxgd6cj5bqrc.s3.us-east-1.amazonaws.com/index_.html)

---

## ✨ Overview

This project is a **serverless AWS application** that converts PDFs into presentation-ready slide decks using:

* 📄 PDF parsing
* 🖼️ Figure extraction
* 🤖 Multimodal + LLM processing
* ☁️ Fully managed AWS services

It leverages **AWS SAM**, **Step Functions**, and **Lambda** to create a scalable, event-driven pipeline.

---

## 🧠 High-Level Design (HLD)

### Architecture Summary

* **Frontend (SPA)** → Upload & interaction
* **API Gateway** → Entry point
* **Lambda Functions** → Business logic
* **Step Functions** → Workflow orchestration
* **S3** → File storage
* **DynamoDB** → Job tracking
* **Hugging Face Models** → AI generation

### 🔷 HLD Diagram

Paste this directly into a Markdown file that supports Mermaid:

```mermaid
flowchart LR
    U[User] --> FE[Frontend SPA]

    FE -->|POST /upload| API[API Gateway]
    FE -->|GET /status/:id| API
    FE -->|POST /preprocess/:id| API
    FE -->|POST /process/:id| API

    API --> CU[Lambda: create_upload]
    API --> ST[Lambda: status]
    API --> SP[Lambda: start_preprocess]
    API --> SPR[Lambda: start_processing]

    CU --> DDB[(DynamoDB JobTable)]
    CU --> S3[(S3 ArtifactBucket)]
    ST --> DDB

    SP --> SM1[Step Functions: Preprocess]
    SPR --> SM2[Step Functions: Process]

    SM1 --> ET[Lambda: extract_text]
    SM1 --> RP[Lambda: render_previews]

    SM2 --> CF[Lambda: crop_figures]
    SM2 --> CM[Lambda: call_model]
    SM2 --> WJ[Lambda: write_final_json]
    SM2 --> CL[Lambda: cleanup_data]

    ET --> S3
    RP --> S3
    CF --> S3
    CM --> S3
    WJ --> S3
    CL --> S3

    ET --> DDB
    RP --> DDB
    CF --> DDB
    CM --> DDB
    WJ --> DDB
    CL --> DDB
```

---

---

## ⚙️ Low-Level Design (LLD)

### Core Workflows

#### 1️⃣ Upload Flow

Paste this directly into a Markdown file that supports Mermaid:

```mermaid
sequenceDiagram
    participant User
    participant FE as Frontend
    participant API as API Gateway
    participant CU as create_upload Lambda
    participant DDB as DynamoDB
    participant S3 as S3

    User->>FE: Select PDF
    FE->>API: POST /upload
    API->>CU: create upload request
    CU->>DDB: Create job record
    CU->>S3: Generate presigned URL
    CU-->>FE: upload_id + upload_url
    FE->>S3: Upload PDF
```

#### 2️⃣ Preprocessing Flow

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as API Gateway
    participant SP as start_preprocess
    participant SM as Step Functions
    participant ET as extract_text
    participant RP as render_previews
    participant DDB as DynamoDB
    participant S3 as S3

    FE->>API: POST /preprocess/{id}
    API->>SP: Start preprocessing
    SP->>SM: Trigger workflow
    SM->>ET: Extract text
    ET->>S3: Save raw.txt
    ET->>DDB: Update status
    SM->>RP: Generate previews
    RP->>S3: Save images
    RP->>DDB: Update previews
```

#### 3️⃣ Processing Flow

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as API Gateway
    participant SPR as start_processing
    participant SM as Step Functions
    participant CF as crop_figures
    participant CM as call_model
    participant WJ as write_final_json
    participant CL as cleanup_data
    participant DDB as DynamoDB
    participant S3 as S3

    FE->>API: POST /process/{id}
    API->>SPR: Start processing
    SPR->>SM: Trigger workflow
    SM->>CF: Crop figures
    CF->>S3: Save crops
    CF->>DDB: Update state
    SM->>CM: Generate slides (AI)
    CM->>S3: Save final.json
    CM->>DDB: Update state
    SM->>WJ: Write viewer
    WJ->>S3: Save index.html
    WJ->>DDB: Save viewer URL
    SM->>CL: Cleanup
    CL->>S3: Delete temp files
    CL->>DDB: Mark complete
```

---

## 🧩 Key Components

| Component          | Role                            |
| ------------------ | ------------------------------- |
| `create_upload`    | Initializes job + presigned URL |
| `extract_text`     | Extracts PDF text               |
| `render_previews`  | Generates page images           |
| `crop_figures`     | Crops user-selected regions     |
| `call_model`       | AI slide generation             |
| `write_final_json` | Publishes final output          |
| `cleanup_data`     | Removes temporary files         |

---

## ☁️ AWS SAM Deployment

### Prerequisites

* AWS CLI configured
* AWS SAM CLI installed
* Python 3.12

### 🔧 Build

```bash
sam build
```

### 🚀 Deploy

```bash
sam deploy --guided
```

During deployment, provide:

* Hugging Face API token
* Stack name
* Region

### 🔁 Subsequent Deployments

```bash
sam deploy
```

---

## 📦 Storage Structure (S3)

```
uploads/<upload_id>/
  ├── original.pdf
  ├── raw.txt
  ├── previews/
  ├── figures/
  ├── final.json
  └── index.html
```

---

## 🔄 State Management (DynamoDB)

Each job tracks:

* `state` (waiting, processing, done)
* `progress`
* `previews`
* `viewer_url`

---

## ⚠️ Notes

* The frontend currently triggers `/process/{upload_id}` immediately after upload (even for preprocessing).
* The backend separates preprocessing and processing workflows.

---

## 💡 Why This Project Matters

* Fully serverless → no infrastructure management
* Scalable pipelines via Step Functions
* Multimodal AI integration
* Real-world document understanding use case

---

## 📜 License

MIT License

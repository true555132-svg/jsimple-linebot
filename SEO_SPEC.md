# SEO 內容管理後台 — 現行系統規格（截至 commit `2a3f865`）

檔案：`seo_admin.py`（單檔 Flask Blueprint，約 3550+ 行），掛載於 `app.py`：
```python
from seo_admin import seo_bp, init_seo_db
app.register_blueprint(seo_bp)
init_seo_db()
```

---

## 1. 環境變數

| 變數 | 用途 | 現況 |
|---|---|---|
| `DATABASE_URL` | Postgres 連線字串 | 已設定 |
| `ADMIN_PASSWORD` | 後台登入密碼（fallback `jsimple2024`） | 已設定 |
| `ANTHROPIC_API_KEY` | AI 分析(Haiku 4.5) + 生成(Sonnet 4.6) | 已設定 |
| `OPENAI_API_KEY` | AI 配圖（`gpt-image-2`） | **尚未設定** |
| `EASYSTORE_ACCESS_TOKEN` | EasyStore Open API 3.0 授權 token | 需確認是否已設定 |
| `EASYSTORE_DOMAIN` | EasyStore 網域（fallback `www.jsimple.tw`） | 有 fallback |
| `EASYSTORE_BLOG_ID_JSIMPLE` | jsimple 品牌對應的 EasyStore blog_id（fallback `164646`） | 有 fallback |
| `GA4_CREDENTIALS_JSON` / `GA4_CREDENTIALS_FILE` / `GA4_PROPERTY_ID` | GA4 成效資料匯入 | 有 fallback |

---

## 2. 資料表（皆為 `CREATE TABLE IF NOT EXISTS`，不影響既有資料）

| 表 | 用途 |
|---|---|
| `seo_titles` | 標題庫（狀態：待寫/已寫/已發布） |
| `seo_articles` | 文章主表（title/slug/meta/content/status + `extra` JSON 欄放擴充資料 + `easystore_article_id`） |
| `seo_tracking` | 成效追蹤（排名/流量/GA4 指標/LINE 詢問/訂單/營收，`source` 欄區分手動或 GA4 匯入） |
| `seo_knowledge` | 品牌知識庫（brand+category+type，供生成文章引用） |
| `seo_opportunities` | AI 主題機會池（SEO/GEO/轉換分數、難度、優先級） |
| `seo_brand_rules` | 品牌SEO規則（定位/客群/主力商品/禁止方向/語氣/CTA/關鍵字，依 brand+category+article_type 唯一） |
| `seo_prompt_templates` | 可在後台編輯的 AI Prompt 模板 |
| `seo_ai_suggestions` | AI 建議快取 |
| 各功能 job 表 | `seo_generate_jobs` `seo_quality_check_jobs` `seo_knowledge_import_jobs` `seo_opportunity_jobs` `seo_ga4_batch_jobs` `seo_link_jobs`（皆為非同步任務輪詢用） |

文章狀態機（`seo_articles.status`）：
`topic_pending → ai_generating → draft_review → (needs_revision) → ready_to_publish → published → needs_optimization / inactive`
（`draft` 為舊資料相容值）

文章類型（`ARTICLE_TYPES`，共 9 種，各自有寫作結構指引）：
資訊型、教學型、比較型、商業導購、FAQ、案例分享、價格分析、尺寸指南、其他

---

## 3. 後台頁面與路由（Sidebar 7 頁）

| 頁面 | 路由 |
|---|---|
| SEO 營運中心 | `GET /admin/seo-dashboard`（+ `POST .../refresh-suggestion`） |
| 文章管理 | `GET /admin/seo`（標題CRUD + 文章列表） |
| 主題機會池 | `GET /admin/seo-opportunities`（+ generate / update / delete） |
| AI 生成文章 | `GET /admin/seo-generator`（+ analyze / preview / generate，皆非同步 job） |
| 知識庫管理 | `GET /admin/seo-knowledge`（CRUD + 批次匯入 import/analyze/confirm） |
| 品牌SEO規則 | `GET /admin/seo-brand-rules`（CRUD + import） |
| Prompt設定 | `GET /admin/seo-settings`（save / reset） |

單篇文章操作（在文章編輯頁 `/admin/seo/article/<id>`）：
- `POST .../save` 儲存
- `POST .../delete` 刪除
- `POST .../publish-easystore` **發布到 EasyStore（已存在，見下）**
- `POST .../suggest-links` AI 內部連結建議
- `POST .../quality-check` AI 14 項品質檢查（0-100分＋建議）
- `GET .../generate-image` AI 配圖生成（`gpt-image-2`，下載 PNG）
- `GET/POST .../tracking`、`.../tracking/add`、`.../tracking/ga4-sync`、`.../tracking/ai-diagnose` 成效追蹤與 AI 診斷

另有對外 API：`GET /api/seo/articles`，`POST /admin/seo/ga4-batch-sync`（批次同步 GA4 排名/流量）。

---

## 4. 已存在的 EasyStore 串接（修正：這不是空白，已經做了）

檔案第 897-950 行，`_easystore_publish_article(aid, scheduled_iso=None)`：
- 用 **EasyStore Open API 3.0**（`/api/3.0/articles.json`，header `EasyStore-Access-Token`）
- 支援**新發布**（POST 到 `articles.json`）與**覆蓋更新已發布文章**（PUT 到 `articles/{id}.json`，用 `seo_articles.easystore_article_id` 判斷）
- 支援排程發布（`scheduled_iso`，轉成台北時區 `+08:00`）
- 目前只設定了 `jsimple` 一個品牌對應的 `blog_id`（`EASYSTORE_BLOG_IDS = {"jsimple": ...}`），其他品牌要發布需要先加對應 blog_id
- 發布成功後寫回 `seo_articles.easystore_article_id`、`status='published'`、`published_at`
- 前端：文章編輯頁「🚀 發布到 EasyStore」按鈕（已發布過會顯示 EasyStore 文章 ID，再按是覆蓋更新）

**這代表「自動發文到 EasyStore」這塊已經有基礎可用**，不是要從零開始。

---

## 5. AI 生成文章流程（`/admin/seo-generator`）

1. 使用者填表單：品牌／品類／主題／主關鍵字／搜尋意圖／目標客群／對應商品／禁止偏離方向／CTA／文章類型（9選1）
2. 「AI 分析」（Haiku 4.5）：依主題產生 7 項建議並自動帶入表單空白欄位——文章類型、主關鍵字、搜尋意圖、目標客群、對應商品、禁止方向、CTA方向（今天剛擴充，原本只有前2項）
3. 套用 `seo_brand_rules`（依 brand+category+article_type 比對優先序）自動補品牌一致性規則
4. 「AI 生成」（Sonnet 4.6，非同步 job）：輸出**純 HTML 格式**文章（非 Markdown），存入 `seo_articles`
5. 生成後可在編輯頁：AI品質檢查 → AI配圖 → AI內部連結建議 → 發布到 EasyStore

## 6. 尚未做的部分

- **公開文章前台**（`/blog/xxx` 實際渲染頁）——文章目前只存在後台資料庫，讀者看到的是發布到 EasyStore 之後、由 EasyStore 自己的部落格模板渲染，本系統本身沒有自己的前台頁面
- **影片製作**——完全沒有，需要重新設計（文字轉影片？現有配圖合成？串第三方工具？）
- Google Search Console 自動匯入（`seo_tracking.source` 欄位已預留，GA4 匯入已做，GSC 還沒）
- 多品牌 EasyStore blog_id 只設了 jsimple 一個

---

## 7. 關於「整合進 EasyStore 後台自動生成文章+發文+影片製作」這個新方向

修正上一版交接紙條的誤植：**發文到 EasyStore 這塊已經做了**（見第4節），不是要從零串接。真正還缺、需要重新想規格的是：
1. **影片製作**——這塊完全空白，是最大的未知數，建議先確定用什麼素材/工具生成
2. 整個流程要不要做成**全自動排程**（例如每天固定從主題池挑題目 → 生成 → 品質檢查通過自動發布），目前每一步都是後台手動點按鈕觸發
3. 是否要讓 EasyStore 後台本身也能觸發（而不是只能從這個獨立的 SEO 後台操作）——如果 EasyStore 沒有開放自訂後台頁面/webhook 機制，這點可能行不通，仍要用這個獨立後台操作、只是發布結果會出現在 EasyStore

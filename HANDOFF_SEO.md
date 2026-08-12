# SEO 內容管理後台 —— 交接紙條（更新版）

專案位置：`e:/shopee-robot/jsimple-linebot`（GitHub: `true555132-svg/jsimple-linebot`，部署在 Render `jsimple-linebot` 服務）

## 現況（本紙條寫成當下）
- GitHub main 最新 commit：`99fd4ab`，本機已同步、無未提交內容，**已 push 但尚未在 Render 手動部署**。
- 這台電腦原本落後 178 個 commit（含 CRM 對話分析儀表板等大量進度），已重新同步。
- 剛合併進去的新功能（commit `2c1191f`）：
  1. **AI 生成配圖**：`OPENAI_API_KEY` 環境變數 + `_build_image_prompt()` + `/admin/seo/article/<id>/generate-image` 路由，文章編輯頁新增「🎨 生成配圖」按鈕，用 `gpt-image-2` 生成部落格首圖並下載（每次約 NT$1-4）。**尚未設定 `OPENAI_API_KEY`，要用需先去 Render Environment 加上。**
  2. **AI 分析建議擴充**：原本分析只給「文章類型／主關鍵字」2 項，現在一次給 7 項，自動帶入搜尋意圖／目標客群／對應商品／禁止方向／CTA方向。

## 已知限制（非本次造成，既有問題）
- `/admin/seo`（文章管理列表頁）在沒有資料庫連線時會 500（既有程式碼，第 4178 行附近 `_q` 查詢 `seo_titles`），有 DB 就正常，暫不處理。

## 架構原則（務必遵守，不要偏離）
- 所有 SEO 後台功能全部寫在單一檔案 `seo_admin.py`，用 Flask Blueprint 掛載
- `app.py` 只允許改 4 行（`from seo_admin import seo_bp` + `app.register_blueprint(seo_bp)`），不碰其他 LINE Bot/FB Bot/商品搬運/CRM 的程式碼
- 每次改完都要：`python -m py_compile seo_admin.py` 語法檢查 → 本機 test_client 模擬測試（無 DB 情況下確認不會 500）→ `git add seo_admin.py`（不要動 app.py）→ commit → push 前先 `git fetch` + `git pull`（這個 repo 常有 LINE Bot 自動上傳圖片產生的 commit，幾乎每次都需要先合併）→ push
- Render 不是自動部署，push 完要手動去 Render Dashboard 點 Manual Deploy
- 後台密碼：環境變數 `ADMIN_PASSWORD`（fallback `jsimple2024`），AI 用 `ANTHROPIC_API_KEY`（已設定，Haiku 4.5 分析 + Sonnet 4.6 生成）

## 下一步／使用者想討論的新方向（尚未設計，回家電腦要重新想規格）
使用者提到想把這套 SEO 後台的邏輯**整合進官網 EasyStore 後台**，目標流程大致是：
> EasyStore 後台 →（自動生成文章）→ 發文 → 影片製作

**訂正：EasyStore 發文串接其實已經做好了**，不是空白——`seo_admin.py` 第897-950行 `_easystore_publish_article()` 已用 EasyStore Open API 3.0 實作發布/覆蓋更新/排程發布，文章編輯頁也有「🚀 發布到 EasyStore」按鈕可用。完整規格見同目錄 `SEO_SPEC.md`。

真正還沒做、需要重新想規格的：
- **影片製作**——完全空白，最大未知數，要先確定素材/工具（文字轉影片？現有AI配圖再合成？串第三方API？）
- 要不要做成全自動排程（現在每一步都要後台手動點按鈕），例如每天自動選題→生成→品檢過關自動發布
- 目前只設定了 jsimple 一個品牌的 EasyStore blog_id，其他品牌要發文需要先補

建議回家電腦先看 `SEO_SPEC.md` 對齊現況，再決定「影片製作」怎麼設計、要不要做自動排程。

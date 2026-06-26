def build_prompt(product_analysis):
    ptype    = product_analysis.get("product_type", "商品")
    style    = product_analysis.get("style", "")
    material = product_analysis.get("material", "")
    space    = product_analysis.get("use_space", "")

    return f"""你是 Google SEO 專家，熟悉台灣消費者搜尋行為。

分析台灣 Google 使用者對以下商品的搜尋意圖：

商品類型：{ptype}
設計風格：{style}
材質：{material}
使用空間：{space}

輸出 JSON（只輸出 JSON，不要其他文字）：
{{
  "primary_keywords": ["主要關鍵字1", "主要關鍵字2", "主要關鍵字3"],
  "long_tail": ["長尾關鍵字1", "長尾關鍵字2", "長尾關鍵字3", "長尾關鍵字4"],
  "search_intent_types": [
    "購買意圖：用戶想要...",
    "比較意圖：用戶想比較...",
    "資訊意圖：用戶想了解..."
  ],
  "featured_snippet_questions": ["適合爭取 Featured Snippet 的問句1", "問句2"],
  "seasonal_notes": "季節性搜尋備注（無則留空）"
}}"""

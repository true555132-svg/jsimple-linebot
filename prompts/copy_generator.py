import json as _json


def build_prompt(brand_profile, product_analysis, search_intent, competitor_analysis):
    brand_name   = brand_profile.get("name", "台灣電商品牌")
    brand_cat    = brand_profile.get("category", "商品")
    positioning  = brand_profile.get("positioning") or brand_profile.get("style", "")
    tone         = brand_profile.get("tone", "直接說明功能，不像業務推銷。")
    forbidden    = brand_profile.get("forbidden_words", "喔、恩、那個、就是說、其實、基本上、保證、一定有效、絕對")
    faq_strategy = brand_profile.get("faq_strategy", "購買前常見疑問")
    copy_length  = brand_profile.get("copy_length", "desc 200-400字，features 3-5點")
    aeo_rules    = brand_profile.get("aeo_rules", "FAQ 問句符合 Google AEO 格式")

    pa_str = _json.dumps(product_analysis,    ensure_ascii=False, indent=2)
    si_str = _json.dumps(search_intent,       ensure_ascii=False, indent=2)
    ca_str = _json.dumps(competitor_analysis, ensure_ascii=False, indent=2)

    return f"""你是「{brand_name}」品牌文案編輯，負責{brand_cat}類商品。

品牌定位：{positioning}
文案口吻：{tone}
文案長度：{copy_length}
禁止用詞：{forbidden}
FAQ 策略：{faq_strategy}
AEO 規則：{aeo_rules}

--- 商品分析 ---
{pa_str}

--- 搜尋意圖 ---
{si_str}

--- 競品分析 ---
{ca_str}

根據以上分析，生成台灣官網風格商品文案。
不要直接翻譯淘寶標題，要重新構思定位。
用具體數字代替模糊描述。不用感嘆號堆疊。

輸出 JSON（只輸出 JSON，不要其他文字）：
{{
  "name": "商品名稱（30字以內，繁體中文，不堆砌關鍵字）",
  "desc": "商品描述（依 copy_length 要求，條列式，繁體中文）",
  "keywords": "關鍵字1,關鍵字2,關鍵字3,關鍵字4,關鍵字5",
  "shopee_title": "蝦皮標題（含關鍵字堆疊風格，40字以內）",
  "website_name": "官網商品名稱（簡潔版，20字以內）",
  "features": "商品特色（條列3-5點，每點一行，用「・」開頭）",
  "price_min": "建議售價下限（純數字，台幣）",
  "price_max": "建議售價上限（純數字，台幣）",
  "seo_desc": "SEO 描述（80字以內，含品牌與主要關鍵字）",
  "faq": [{{"q": "問題1", "a": "具體回答1"}}, {{"q": "問題2", "a": "回答2"}}, {{"q": "問題3", "a": "回答3"}}]
}}"""

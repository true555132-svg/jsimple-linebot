def build_prompt(raw_title, raw_desc, raw_price=""):
    parts = []
    if raw_title: parts.append(f"原始標題：{raw_title}")
    if raw_price: parts.append(f"參考價格：{raw_price}")
    if raw_desc:  parts.append(f"原始描述：{raw_desc[:2000]}")
    product_block = "\n".join(parts)

    return f"""你是商品分析專家，熟悉台灣電商市場。分析以下淘寶商品，輸出 JSON。

{product_block}

輸出格式（只輸出 JSON，不要其他文字）：
{{
  "product_type": "商品類型（例：餐椅、書桌、吊燈）",
  "material": "主要材質（例：鐵管+麻布、實木+不鏽鋼）",
  "style": "設計風格（例：工業風、北歐風、現代簡約）",
  "use_space": "使用空間（例：餐廳、臥室、書房、多用途）",
  "target_audience": "適合客群（例：租屋族、小坪數、重視品質的家庭）",
  "features": ["功能特色1", "功能特色2", "功能特色3"],
  "customizable": false,
  "assembly_required": true,
  "size_highlights": "尺寸重點（例：座高45cm、承重150kg）",
  "selling_points": ["最強賣點1", "最強賣點2", "最強賣點3"],
  "price_tier": "低/中/中高/高"
}}"""

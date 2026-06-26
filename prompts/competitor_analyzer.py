def build_prompt(product_analysis):
    ptype      = product_analysis.get("product_type", "商品")
    material   = product_analysis.get("material", "")
    features   = product_analysis.get("features", [])
    points     = product_analysis.get("selling_points", [])
    price_tier = product_analysis.get("price_tier", "中")

    features_str = "、".join(features) if features else "無"
    points_str   = "、".join(points)   if points   else "無"

    return f"""你是電商競品分析師，熟悉台灣居家消費市場。

分析以下商品的市場競爭狀況：

商品類型：{ptype}
材質：{material}
功能特色：{features_str}
主要賣點：{points_str}
價格區間：{price_tier}價位

輸出 JSON（只輸出 JSON，不要其他文字）：
{{
  "main_competition_axis": "主要競爭維度（例：價格、外觀、材質）",
  "advantages": ["相對市場優勢1", "相對市場優勢2"],
  "differentiation": "差異化建議（如何在文案中突出）",
  "positioning_angle": "建議的文案定位角度",
  "key_objections": ["消費者購買前最可能的顧慮1", "顧慮2"]
}}"""

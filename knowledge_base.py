"""
J SIMPLE 高架床 Bot 知識庫
"""

BRAND_INFO = {
    "line_id": "@JSIMPLE",
    "website": "https://www.jsimple.tw/collections/loft-bed",
    "price_range": "NT$7,000～NT$15,000",
    "warranty": "三年",
    "delivery_stock": "現貨 2～5 天",
    "delivery_custom": "訂製 4～6 週",
}

SHIPPING = {"north":1000,"central_south":1300,"floor_surcharge":300,"elevator_surcharge":300}

LINE_ENABLED = True
FB_ENABLED = True

INTENT_LABELS = {
    "greeting": """打招呼""",
    "price": """價格詢問""",
    "custom": """訂製/客製""",
    "shipping": """運費/安裝""",
    "size": """尺寸詢問""",
    "delivery": """出貨時間""",
    "warranty": """保固""",
    "material": """材質/安全""",
    "color": """顏色/款式""",
    "payment": """付款方式""",
    "return": """退換貨""",
    "default": """預設回覆""",
}

LINE_REPLIES = {
    "greeting": """您好！感謝您聯繫 J Simple 🙌

我們專注於：
🛏️ 高架床 / 架高床
🪑 辦公傢俱 / 書桌椅
📦 系統收納 / 訂製傢俱

請問您想了解哪個品項呢？
1️⃣ 高架床（現貨 / 訂製）
2️⃣ 辦公桌椅
3️⃣ 其他傢俱

👉 直接告訴我需求，我們幫您找最適合的方案！
""",
    "price": """高架床價格範圍：NT$7,000～$15,000 😊

依尺寸與配置不同：
🔹 標準現貨款：NT$7,000 起
🔹 步梯收納款：NT$9,000 起
🔹 KY 訂製款：依規格報價

建議加 LINE 讓我們幫您抓最適合的方案：""",
    "custom": """可以的唷～KY 系列全系列支援客製 🎉

✅ 可調整項目：
・床體長寬（3尺～6尺）
・床板離地高度（145～180cm）
・梯子方向（左 / 右）
・護欄高度、下方功能配置

因為訂製需要確認房間尺寸與動線，
建議直接加 LINE 傳照片＋尺寸，
我們一次幫您評估與初估報價 👍

👉 https://www.jsimple.tw/collections/loft-bed""",
    "shipping": """運費與安裝費用說明：

🚚 運費：
・北部：NT$1,000
・中部 / 南部：NT$1,300

🔧 搬運費：
・無電梯：每層加收 NT$300
・有電梯：一次搬運費 NT$300

如需精確報價，請提供：
➤ 安裝地址與樓層
➤ 是否有電梯""",
    "size": """高架床尺寸說明 📐

標準尺寸：
・單人（3尺）：90×190cm
・加大單人：105×195cm
・雙人（5尺）：150×200cm
・雙人加大（6尺）：180×200cm

✅ 全系列支援客製長寬，空間不夠也能訂做！

建議提供天花板高度 + 房間尺寸，
我們幫您規劃最適合的配置 😊""",
    "delivery": """出貨時間說明 📦

・現貨款：付款後 2～5 個工作天出貨
・訂製款：4～6 週（依規格複雜度）

如需確認特定款式是否有現貨，
歡迎加 LINE 詢問 😊

👉 LINE@：@JSIMPLE""",
    "warranty": """保固說明 🛡️

全系列高架床提供 3 年結構保固。

保固範圍：
・鋼管結構、焊接點
・五金配件（螺絲、插銷）

不含：人為損壞、超重使用

有其他問題歡迎詢問 😊""",
    "material": """材質與結構說明 🔩

・主體：加厚方管鋼材（50x50mm）
・表面：靜電粉體烤漆，防鏽耐用
・板材：E1 低甲醛環保板
・承重：床板設計承重 300kg 以上

結構穩固，不會晃動 💪
""",
    "color": """目前款式顏色 🎨

主力款式：
・工業黑（最熱銷）
・霧白
・原木黑鐵（搭配木紋板）

訂製款可依需求調整配色。

想看實際圖片可以到官網或加 LINE 索取：
👉 https://www.jsimple.tw/collections/loft-bed
👉 LINE@：@JSIMPLE""",
    "payment": """付款方式說明 💳

✅ 支援：
・信用卡（一次付清）
・ATM 轉帳
・LINE Pay
・貨到付款（限特定區域）

目前暫不提供分期服務。

有其他問題歡迎詢問 😊""",
    "return": """退換貨說明 📋

・收到商品 7 天內，外觀完整可申請退換
・訂製款：因依需求製作，恕不接受退換
・如有瑕疵或損壞，我們全力負責處理

有任何問題請直接聯繫我們""",
    "default": """感謝您的詢問 😊

為了更快幫您確認需求，
建議直接加我們的 LINE，
傳房間尺寸或照片，
我們會直接幫您評估與建議 👍

👉 LINE@：@JSIMPLE
👉 https://www.jsimple.tw/collections/loft-bed""",
}

FB_REPLIES = {
    "greeting": """您好～感謝詢問高架床 😊
我們有多種款式與訂製配置，

🔹 價格：NT$7,000～$15,000
🔹 結構：加厚鋼材結構
🔹 保固：三年
🔹 交期：現貨 2～5 天 / 訂製 4～6 週

請問您的需求是：
1️⃣ 現貨款
2️⃣ 訂製款（客製尺寸）
3️⃣ 想先看款式
👉 LINE@：@JSIMPLE
（加 LINE 後傳房間尺寸或照片即可）
👉 款式頁面：https://www.jsimple.tw/collections/loft-bed""",
    "price": """高架床價格範圍：NT$7,000～$15,000 😊

依尺寸與配置不同：
🔹 標準現貨款：NT$7,000 起
🔹 步梯收納款：NT$9,000 起
🔹 KY 訂製款：依規格報價

建議加 LINE 讓我們幫您抓最適合的方案：""",
    "custom": """可以的唷～KY 系列全系列支援客製 🎉

✅ 可調整項目：
・床體長寬（3尺～6尺）
・床板離地高度（145～180cm）
・梯子方向（左 / 右）
・護欄高度、下方功能配置

因為訂製需要確認房間尺寸與動線，
建議直接加 LINE 傳照片＋尺寸，
我們一次幫您評估與初估報價 👍
👉 LINE@：@JSIMPLE
👉 https://www.jsimple.tw/collections/loft-bed""",
    "shipping": """運費與安裝費用說明：

🚚 運費：
・北部：NT$1,000
・中部 / 南部：NT$1,300

🔧 搬運費：
・無電梯：每層加收 NT$300
・有電梯：一次搬運費 NT$300

如需精確報價，請提供：
➤ 安裝地址與樓層
➤ 是否有電梯

👉 LINE@：@JSIMPLE
👉 https://www.jsimple.tw/collections/loft-bed """,
    "size": """高架床尺寸說明 📐

標準尺寸：
・單人（3尺）：90×190cm
・加大單人：105×195cm
・雙人（5尺）：150×200cm
・雙人加大（6尺）：180×200cm

✅ 全系列支援客製長寬，空間不夠也能訂做！

建議提供天花板高度 + 房間尺寸，
我們幫您規劃最適合的配置 😊
建議直接加我們的「高架床訂製 LINE」，
可以一次幫您評估是否合適 👍

👉 LINE@：@JSIMPLE
（加 LINE 後傳房間尺寸或照片即可）
👉 https://www.jsimple.tw/collections/loft-bed """,
    "delivery": """出貨時間說明 📦

・現貨款：付款後 2～5 個工作天出貨
・訂製款：4～6 週（依規格複雜度）

如需確認特定款式是否有現貨，
建議直接加我們的「高架床訂製 LINE」，
可以一次幫您評估是否合適 👍
👉 LINE@：@JSIMPLE
（加 LINE 後傳房間尺寸或照片即可）
👉 https://www.jsimple.tw/collections/loft-bed """,
    "warranty": """保固說明 🛡️

全系列高架床提供 3 年結構保固。

保固範圍：
・鋼管結構、焊接點
・五金配件（螺絲、插銷）

不含：人為損壞、超重使用

有其他問題歡迎詢問 😊
👉 LINE@：@JSIMPLE
（加 LINE 後傳房間尺寸或照片即可）
👉 https://www.jsimple.tw/collections/loft-bed """,
    "material": """材質與結構說明 🔩

・主體：加厚方管鋼材（50x50mm）
・表面：靜電粉體烤漆，防鏽耐用
・板材：E1 低甲醛環保板
・承重：床板設計承重 300kg 以上

結構穩固，不會晃動 💪


👉 LINE@：@JSIMPLE
（加 LINE 後傳房間尺寸或照片即可）
👉 https://www.jsimple.tw/collections/loft-bed """,
    "color": """目前款式顏色 🎨

主力款式：
・工業黑（最熱銷）
・霧白
・原木黑鐵（搭配木紋板）

訂製款可依需求調整配色。

想看實際圖片可以到官網或加 LINE 索取：
👉 https://www.jsimple.tw/collections/loft-bed
👉 LINE@：@JSIMPLE""",
    "payment": """付款方式說明 💳

✅ 支援：
・信用卡（一次付清）
・ATM 轉帳
・LINE Pay
・貨到付款（限特定區域）

目前暫不提供分期服務。

有其他問題歡迎詢問 😊""",
    "return": """退換貨說明 📋

・收到商品 7 天內，外觀完整可申請退換
・訂製款：因依需求製作，恕不接受退換
・如有瑕疵或損壞，我們全力負責處理

有任何問題請直接聯繫我們""",
    "default": """感謝您的詢問 😊

為了更快幫您確認需求，
建議直接加我們的 LINE，
傳房間尺寸或照片，
我們會直接幫您評估與建議 👍

👉 LINE@：@JSIMPLE
👉 https://www.jsimple.tw/collections/loft-bed""",
}

LINE_KEYWORDS = {
    "price": ["價格", "多少錢", "費用", "報價", "貴不貴", "預算", "幾千", "便宜", "優惠", "折扣", "特價"],
    "custom": ["訂製", "客製", "客製化", "尺寸訂做", "特殊尺寸", "訂做", "可以改", "能不能改", "調整"],
    "shipping": ["運費", "運送", "安裝費", "搬運", "配送", "送貨", "怎麼安裝", "自己裝", "組裝", "師傅"],
    "size": ["尺寸", "幾尺", "多大", "寬度", "長度", "高度", "天花板", "幾公分", "cm", "幾米", "空間", "放得下"],
    "delivery": ["幾天", "出貨", "交期", "到貨", "多久", "等多久", "快嗎", "現貨", "庫存", "有貨", "現在有"],
    "warranty": ["保固", "保證", "壞掉", "維修", "故障", "生鏽", "鏽", "螺絲鬆"],
    "material": ["材質", "鋼管", "鐵", "木", "板材", "幾mm", "厚度", "堅固", "穩", "晃", "承重", "幾公斤", "kg", "重量限制", "安全"],
    "color": ["顏色", "黑色", "白色", "什麼色", "有哪些色", "款式", "外觀", "樣式", "型號"],
    "payment": ["付款", "匯款", "刷卡", "信用卡", "轉帳", "分期", "Line Pay", "linepay", "pay", "怎麼付"],
    "return": ["退貨", "換貨", "退款", "不喜歡", "不符合", "取消", "退"],
    "greeting": ["你好", "您好", "hi", "hello", "詢問", "想問", "請問", "想了解", "看一下", "查詢"],
}

FB_KEYWORDS = {
    "price": ["價格", "多少錢", "費用", "報價", "貴不貴", "預算", "幾千", "便宜", "優惠", "折扣", "特價"],
    "custom": ["訂製", "客製", "客製化", "尺寸訂做", "特殊尺寸", "訂做", "可以改", "能不能改", "調整"],
    "shipping": ["運費", "運送", "安裝費", "搬運", "配送", "送貨", "怎麼安裝", "自己裝", "組裝", "師傅"],
    "size": ["尺寸", "幾尺", "多大", "寬度", "長度", "高度", "天花板", "幾公分", "cm", "幾米", "空間", "放得下"],
    "delivery": ["幾天", "出貨", "交期", "到貨", "多久", "等多久", "快嗎", "現貨", "庫存", "有貨", "現在有"],
    "warranty": ["保固", "保證", "壞掉", "維修", "故障", "生鏽", "鏽", "螺絲鬆"],
    "material": ["材質", "鋼管", "鐵", "木", "板材", "幾mm", "厚度", "堅固", "穩", "晃", "承重", "幾公斤", "kg", "重量限制", "安全"],
    "color": ["顏色", "黑色", "白色", "什麼色", "有哪些色", "款式", "外觀", "樣式", "型號"],
    "payment": ["付款", "匯款", "刷卡", "信用卡", "轉帳", "分期", "Line Pay", "linepay", "pay", "怎麼付"],
    "return": ["退貨", "換貨", "退款", "不喜歡", "不符合", "取消", "退"],
    "greeting": ["你好", "您好", "hi", "hello", "詢問", "想問", "請問", "想了解", "看一下", "查詢"],
}

LINE_IMAGE_URLS = {
}

FB_IMAGE_URLS = {
    "custom": """https://raw.githubusercontent.com/true555132-svg/jsimple-linebot/main/images/1776919927_unnamed.png""",
}

LINE_ENABLED_INTENTS = {
    "greeting": True,
    "price": False,
    "custom": False,
    "shipping": False,
    "size": False,
    "delivery": False,
    "warranty": False,
    "material": False,
    "color": False,
    "payment": False,
    "return": False,
    "default": False,
}

FB_ENABLED_INTENTS = {
    "greeting": True,
    "price": False,
    "custom": True,
    "shipping": True,
    "size": True,
    "delivery": True,
    "warranty": True,
    "material": True,
    "color": False,
    "payment": True,
    "return": True,
    "default": True,
}

_BASE_REPLIES = LINE_REPLIES
_BASE_KEYWORDS = LINE_KEYWORDS

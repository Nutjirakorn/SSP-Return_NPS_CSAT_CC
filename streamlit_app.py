"""
Supersports CX Dashboard: Returns, NPS & CSAT  (streamlit_app.py v1, 5 Nov 2026)

Reads the Google Sheet "CX_SSP_Return From & NPS CSAT" (tabs Return Form, NPS_CSAT)
and refreshes automatically every REFRESH_MINUTES.

Access (pick one):
  A) Service account (recommended, keeps the sheet private):
     put the service account JSON in .streamlit/secrets.toml under [gcp_service_account]
     and share the sheet (Viewer) with the service account's client_email.
  B) Public link: set the sheet to "Anyone with the link can view".
     WARNING: the Return Form tab holds bank account numbers, names and addresses.
     Anyone with the link could read them. Use A unless the sheet is cleaned first.

The app drops bank, address, email, phone and name columns right after loading.
"""
import io
import re
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
SHEET_ID = "1PyufqIJtxbhtCMS_iTySSOZbT3_4sTj4xBKP8SF8r8Q"
GID_RETURN = "0"            # tab "Return Form"
GID_NPS = "1890669382"      # tab "NPS_CSAT"
REFRESH_MINUTES = 5          # how often the app re-reads the sheet

# Core KPIs: dir 1 = higher is better, -1 = lower is better
KPI = {
    "csat":   dict(name="CSAT (score 4 to 5)", src="survey", dir=1, ex=0.95, min=0.90, pct=True, basis="SSP CS standard"),
    "nps":    dict(name="NPS", src="survey", dir=1, ex=60, min=50, pct=False, basis="Proposed"),
    "det":    dict(name="Detractors (NPS 0 to 6)", src="survey", dir=-1, ex=0.10, min=0.15, pct=True, basis="Proposed, at most"),
    "seller": dict(name="Returns caused by us", src="returns", dir=-1, ex=0.10, min=0.15, pct=True, basis="Proposed, at most"),
    "fulfil": dict(name="Wrong size or item sent", src="returns", dir=-1, ex=0.03, min=0.05, pct=True, basis="Proposed, at most"),
    "size":   dict(name="Size and fit returns", src="returns", dir=-1, ex=0.60, min=0.70, pct=True, basis="Proposed, at most"),
}

REASON_MAP = {  # raw reason -> (label, group)
    "Items is too small": ("Item too small", "Size & fit"),
    "Items is too big": ("Item too big", "Size & fit"),
    "Order multiple sizes to try on": ("Ordered several sizes to try", "Size & fit"),
    "Wrong measurement on website": ("Wrong measurement on website", "Website content"),
    "Items looks different from the website": ("Looks different from website", "Website content"),
    "Wrong size received": ("Wrong size received", "Fulfilment error"),
    "Wrong item received": ("Wrong item received", "Fulfilment error"),
    "Defective item": ("Defective item", "Product quality"),
    "Quality is not up to expection": ("Quality not up to expectation", "Product quality"),
    "Parcel is Damaged on arrival": ("Parcel damaged on arrival", "Product quality"),
    "Change of mind": ("Change of mind", "Change of mind"),
}
SELLER_GROUPS = {"Website content", "Fulfilment error", "Product quality"}
GROUP_ORDER = ["Size & fit", "Change of mind", "Website content", "Fulfilment error", "Product quality", "Not stated"]
METHOD_MAP = {
    "Our Stores (Free)": "Return at store", "Drop at Supersports Store (Free)": "Drop at store",
    "Parcel Delivery": "Parcel (customer pays)", "Knock Door Service (Free)": "Knock door pickup",
}
PAY_MAP = {"Bank transfer": "Bank transfer", "Cash on Delivery (COD)": "COD", "Credit card or Debit card": "Card"}

AREA_EN = {  # NPS improvement area (Thai, as in the sheet) -> English
    "ระบบ The 1": "The 1 membership", "คุณภาพสินค้า": "Product quality", "การคืนสินค้าและคืนเงิน": "Returns and refunds",
    "การสั่งซื้อและชำระเงิน": "Ordering and payment", "การเลือกรับสินค้าที่สาขา": "Store pickup (Click & Collect)",
    "การจัดส่งสินค้าที่บ้าน": "Home delivery", "การใช้งานบนหน้าเว็ปไซต์ หรือแอปพลิเคชั่น": "Website / app use",
    "ระบบการแจ้งเตือน": "Notifications", "การยกเลิกคำสั่งซื้อ": "Order cancellation",
    "การค้นหาสินค้า และการแบ่งหมวดหมู่สินค้า": "Product search and categories", "ข้อมูล รายละเอียดสินค้า": "Product information",
    "ความหลากหลายของสินค้าและแบรนด์": "Product and brand range", "สินค้าที่ท่านต้องการให้มีพร้อมจำหน่าย": "Products wanted in stock",
    "-": "Nothing to improve",
}

LOW_THEMES = [  # keyword themes for low score reasons (Thai + English)
    ("Delivery speed / not received", r"ส่งช้า|จัดส่งช้า|ได้รับช้า|ล่าช้า|ช้า|นานมาก|รอนาน|ยังไม่ได้รับ|ไม่ได้รับสินค้า|delay|late|slow|not receiv|ขนส่ง|kerry"),
    ("Order cancelled / out of stock", r"ยกเลิก|cancel|stock|สต็อก|สต๊อก|สินค้าหมด|ของหมด|ไม่มีสินค้า|sold out"),
    ("Refund / return", r"คืนเงิน|เงินคืน|refund|นโยบาย|policy|exchange|คืนสินค้า|เปลี่ยนสินค้า|return|เปลี่ยนไซส์"),
    ("Size / fit", r"ไซส์|ไซซ์|ไซด์|ไซร้|size|ขนาด|คับ|หลวม"),
    ("Product quality / wrong item", r"ชำรุด|เสีย|ตำหนิ|ไม่ครบ|ได้ของผิด|เลอะ|หลุด|ร่อน|ไม่สมบูรณ์|มีปัญหา|defect|damag|ส่งผิด|ไม่ตรง|ปลอม|fake|คุณภาพ|quality|wrong|เก่า"),
    ("Price / promotion / coupon", r"ราคา|แพง|ส่วนลด|โค้ด|โค๊ด|คูปอง|โปร|price|discount|code|voucher|coupon|promo"),
    ("Customer service contact", r"ติดต่อ|call ?center|แอดมิน|admin|พนักงาน|ตอบช้า|ไม่ตอบ|แชท|chat|โทร|service|บริการ"),
    ("App / website / payment", r"แอป|แอพ|app|เว็บ|web|ระบบ|login|ล็อกอิน|ชำระ|จ่ายเงิน|payment|ค้นหา|search|โหลด"),
    ("Packaging", r"กล่อง|แพ็ค|แพค|บรรจุ|box|packag"),
    ("Store pickup (Click & Collect)", r"สาขา|รับที่ร้าน|pick ?up|click ?& ?collect"),
    ("The 1 points", r"the ?1|เดอะวัน|คะแนน|point"),
]
LOW_THEMES_RE = [(n, re.compile(p, re.I)) for n, p in LOW_THEMES]

STORE_RULES = [  # (store, keywords) checked in order on lowercase text without spaces
    ("Central World", ["centralworld", "เซ็นทรัลเวิลด์", "เซนทรัลเวิลด์", "ctw", "เวิลด์", "world"]),
    ("Mega Bangna", ["megabangna", "เมกาบางนา", "เมกะบางนา", "เมกกะบางนา", "mega"]),
    ("Central Ladprao", ["ladprao", "ladpraw", "ladplao", "lardprao", "ladphrao", "ลาดพร้าว"]),
    ("Central Rama 3", ["rama3", "พระราม3", "พระรามสาม"]),
    ("Central Rama 9", ["rama9", "พระราม9", "พระรามเก้า", "praram9"]),
    ("Central Rama 2", ["rama2", "พระราม2", "praram2"]),
    ("Central EastVille", ["eastvil", "eastview", "อีสต์วิล", "อีสวิล"]),
    ("Central WestVille", ["westvil", "เวสต์วิล", "เวสวิล"]),
    ("Central WestGate", ["westgate", "เวสเกต", "เวสต์เกต"]),
    ("Central Pinklao", ["pinklao", "ปิ่นเกล้า"]),
    ("Central Bangna", ["centralbangna", "เซ็นทรัลบางนา", "เซนทรัลบางนา"]),
    ("Fashion Island", ["fashion", "fasion", "แฟชั่น"]),
    ("Seacon Bangkae", ["bangkae", "bangkhae", "บางแค"]),
    ("Seacon Square Srinakarin", ["seacon", "ซีคอน", "ศรีนครินทร์"]),
    ("Central Chidlom", ["chidlom", "chitlom", "ชิดลม"]),
    ("Silom Complex", ["silom", "สีลม"]),
    ("MBK Center", ["mbk", "มาบุญครอง"]),
    ("Future Park Rangsit", ["futurepark", "ฟิวเจอร์", "รังสิต", "rangsit"]),
    ("Central Chaengwattana", ["chaengwattana", "changwattana", "แจ้งวัฒนะ"]),
    ("Central Ramintra", ["ramintra", "ramindra", "รามอินทรา"]),
    ("Central Salaya", ["salaya", "ศาลายา"]),
    ("Central Bangrak", ["bangrak", "บางรัก"]),
    ("Central Mahachai", ["mahachai", "มหาชัย"]),
    ("Robinson Sukhumvit", ["robinsonsukhumvit", "โรบินสันสุขุมวิท"]),
    ("Robinson Srisaman", ["srisaman", "srisamarn", "ศรีสมาน"]),
    ("Robinson Ladkrabang", ["ladkrabang", "latkrabang", "ลาดกระบัง"]),
    ("Robinson Samut Prakan", ["samutprakan", "samutprakarn", "สมุทรปราการ"]),
    ("Central Festival Chiang Mai", ["festivalchiangmai", "เฟสติวัลเชียงใหม่", "เชียงใหม่3"]),
    ("Central Chiang Mai Airport", ["airport", "แอร์พอร์ต", "เชียงใหม่2"]),
    ("Central Chiang Mai (unspecified)", ["chiangmai", "เชียงใหม่"]),
    ("Central Chiang Rai", ["chiangrai", "เชียงราย"]),
    ("Central Festival Phuket", ["festivalphuket", "phuketfestival", "เฟสติวัลภูเก็ต"]),
    ("Central Phuket", ["phuket", "ภูเก็ต"]),
    ("Central Pattaya", ["pattaya", "พัทยา"]),
    ("Central Chonburi", ["chonburi", "ชลบุรี"]),
    ("Central Sriracha", ["sriracha", "ศรีราชา"]),
    ("Central Rayong", ["rayong", "ระยอง"]),
    ("Central Khon Kaen", ["khonkaen", "ขอนแก่น"]),
    ("Central Korat", ["korat", "โคราช", "นครราชสีมา"]),
    ("Central Udon Thani", ["udon", "อุดร"]),
    ("Central Ubon Ratchathani", ["ubon", "อุบล"]),
    ("Central Hatyai", ["hatyai", "หาดใหญ่"]),
    ("Central Samui", ["samui", "สมุย"]),
    ("Central Surat Thani", ["surat", "สุราษฎร์"]),
    ("Central Nakhon Pathom", ["nakhonpathom", "นครปฐม"]),
    ("Central Nakhon Sawan", ["nakhonsawan", "นครสวรรค์"]),
    ("Central Phitsanulok", ["phitsanulok", "พิษณุโลก"]),
    ("Central Lampang", ["lampang", "ลำปาง"]),
    ("Central Ayutthaya", ["ayutthaya", "อยุธยา"]),
    ("Central Chanthaburi", ["chanthaburi", "chantaburi", "จันทบุรี"]),
    ("Market Village Hua Hin", ["huahin", "หัวหิน"]),
    ("Robinson Saraburi", ["saraburi", "สระบุรี"]),
    ("Robinson Ratchaburi", ["ratchaburi", "ราชบุรี"]),
    ("Robinson Roi Et", ["roiet", "ร้อยเอ็ด"]),
    ("Robinson Mukdahan", ["mukdahan", "มุกดาหาร"]),
]

st.set_page_config(page_title="Supersports CX Dashboard", page_icon="📊", layout="wide")


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def _export_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"


def _fetch_csv(gid: str) -> str:
    """Download one tab as CSV text, with a service account if configured, else the public link."""
    url = _export_url(gid)
    try:
        has_sa = "gcp_service_account" in st.secrets
    except Exception:  # noqa: BLE001  (no secrets.toml at all)
        has_sa = False
    if has_sa:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        resp = AuthorizedSession(creds).get(url, timeout=120)
    else:
        resp = requests.get(url, timeout=120)
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or "text/html" in ctype:
        raise PermissionError(
            "Google did not return the sheet. Share it with the service account in secrets "
            "(recommended) or set it to 'Anyone with the link can view'."
        )
    resp.encoding = "utf-8"
    return resp.text


def _col(df: pd.DataFrame, prefix: str) -> list:
    """All column positions whose header starts with prefix (headers repeat for each return method)."""
    p = prefix.lower()
    return [i for i, c in enumerate(df.columns) if str(c).strip().lower().startswith(p)]


def _coalesce(df: pd.DataFrame, positions: list) -> pd.Series:
    out = pd.Series([""] * len(df), index=df.index, dtype=object)
    for i in positions:
        v = df.iloc[:, i].fillna("").astype(str).str.strip()
        out = out.where(out != "", v)
    return out


def norm_store(raw: str) -> str:
    s = re.sub(r"[\s\-_.,()/]+", "", str(raw or "").lower())
    s = s.replace("supersports", "").replace("supersport", "").replace("สาขา", "")
    if not s:
        return ""
    for name, keys in STORE_RULES:
        if any(k in s for k in keys):
            return name
    return "Other / unclear"


def low_themes(text: str) -> list:
    t = str(text or "")
    hits = [n for n, p in LOW_THEMES_RE if p.search(t)]
    if hits:
        return hits
    return ["No reason given"] if len(re.sub(r"[\s\-.]", "", t)) < 3 else ["Other"]


def scrub(t: str) -> str:
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", str(t or ""))
    t = re.sub(r"(\+?66|0)[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{3,4}", "[phone]", t)
    return re.sub(r"\b\d{9,}\b", "[number]", t).strip()


def clean_returns(raw: pd.DataFrame) -> tuple:
    log = {"Return Form rows in sheet": len(raw)}
    df = pd.DataFrame(index=raw.index)
    df["ts"] = pd.to_datetime(raw.iloc[:, _col(raw, "Start time")[0]], format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
    reason_raw = _coalesce(raw, _col(raw, "Return Reason"))
    df["reason"] = reason_raw.map(lambda r: REASON_MAP.get(r, (r or "Not stated", "Not stated"))[0])
    df["group"] = reason_raw.map(lambda r: REASON_MAP.get(r, ("", "Not stated"))[1])
    df["method"] = _coalesce(raw, list(reversed(_col(raw, "Select your return method")))).map(lambda m: METHOD_MAP.get(m, "Not stated"))
    store_raw = _coalesce(raw, _col(raw, "Which Supersports branch"))
    df["store"] = [norm_store(s) if m in ("Return at store", "Drop at store") else "" for s, m in zip(store_raw, df["method"])]
    df.loc[df["method"].isin(["Return at store", "Drop at store"]) & (df["store"] == ""), "store"] = "Other / unclear"
    items = pd.to_numeric(_coalesce(raw, _col(raw, "How many items")), errors="coerce")
    df["items"] = items.where(items.between(1, 20))
    scope = _coalesce(raw, _col(raw, "How do you want to return"))
    df["scope"] = scope.map(lambda v: "Whole order" if "whole" in v.lower() else "Some items" if v else "Not stated")
    df["payment"] = _coalesce(raw, _col(raw, "Which payment method")).map(lambda v: PAY_MAP.get(v, "Not stated"))
    order = _coalesce(raw, _col(raw, "Please provide your 6-digit order")).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    df["comment"] = _coalesce(raw, _col(raw, "If you have any comments")).map(scrub)
    n0 = len(df)
    df = df[df["ts"].notna()].copy()
    log["Return rows dropped: no or unreadable date"] = n0 - len(df)
    df["date"] = df["ts"].dt.date
    df = df.sort_values("ts")
    order = order.loc[df.index]
    df["repeat"] = order.ne("") & order.duplicated()
    log["Return rows used"] = len(df)
    log["Return rows with no reason"] = int((df["group"] == "Not stated").sum())
    log["Repeat forms (order already submitted)"] = int(df["repeat"].sum())
    return df, log


def clean_survey(raw: pd.DataFrame) -> tuple:
    log = {"NPS_CSAT rows in sheet": len(raw)}
    c_csat = _col(raw, "ท่านพึงพอใจ")[0]
    c_nps = _col(raw, "มีแนวโน้มแค่ไหน")[0]
    raw = raw[~raw.iloc[:, c_csat].astype(str).str.contains("Response", na=False)]  # SurveyMonkey sub header row
    log["Sub header rows removed"] = log["NPS_CSAT rows in sheet"] - len(raw)
    df = pd.DataFrame(index=raw.index)
    df["ts"] = pd.to_datetime(raw.iloc[:, _col(raw, "Start Date")[0]], format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
    csat = pd.to_numeric(raw.iloc[:, c_csat], errors="coerce")
    df["csat"] = csat.where(csat.between(1, 5))
    nps = pd.to_numeric(raw.iloc[:, c_nps], errors="coerce")
    df["nps"] = nps.where(nps.between(0, 10))
    df["platform"] = raw.iloc[:, _col(raw, "ลูกค้าเข้าใช้งานจากแพลตฟอร์มใด")[0]].fillna("").replace(
        {"mobile app": "Mobile app", "website mobile": "Website mobile", "website desktop": "Website desktop", "": "Not stated"})
    ch_pos = _col(raw, "ท่านได้รับแบบสอบถามนี้จากช่องทางใด")
    df["channel"] = raw.iloc[:, ch_pos[0]].fillna("").replace({"อีเมล": "Email", "": "Not stated"}) if ch_pos else "Not stated"
    # improvement areas: 15 tick-box columns starting at the multi choice question
    a0 = _col(raw, "การซื้อสินค้ากับ Supersports Online")[0]
    area_cols = raw.iloc[:, a0:a0 + 15].fillna("").astype(str)
    df["areas"] = area_cols.apply(lambda r: [AREA_EN.get(v.strip(), v.strip()) for v in r if v.strip()], axis=1)
    reason = raw.iloc[:, _col(raw, "อะไรคือเหตุผลหลัก")[0]].fillna("").astype(str)
    msg_pos = _col(raw, "ฝากข้อความถึงเรา")
    msg = raw.iloc[:, msg_pos[0]].fillna("").astype(str) if msg_pos else ""
    df["text"] = (reason + " / " + msg).map(scrub).str.strip(" /")
    df["low"] = (df["csat"] <= 3) | (df["nps"] <= 6)
    df["themes"] = [low_themes(t) if lo else [] for t, lo in zip(df["text"], df["low"])]
    n0 = len(df)
    df = df[df["ts"].notna()].copy()
    log["Survey rows dropped: no or unreadable date"] = n0 - len(df)
    df["date"] = df["ts"].dt.date
    log["Survey rows used"] = len(df)
    log["CSAT outside 1 to 5 (e.g. 0), left out of CSAT"] = int(df["csat"].isna().sum())
    return df.sort_values("ts"), log


@st.cache_data(ttl=REFRESH_MINUTES * 60, show_spinner="Reading the Google Sheet...")
def load_data():
    ret_raw = pd.read_csv(io.StringIO(_fetch_csv(GID_RETURN)), dtype=str, keep_default_na=False)
    nps_raw = pd.read_csv(io.StringIO(_fetch_csv(GID_NPS)), dtype=str, keep_default_na=False)
    r, log_r = clean_returns(ret_raw)
    s, log_s = clean_survey(nps_raw)
    loaded = pd.Timestamp.now(tz="Asia/Bangkok").strftime("%-d %b %Y %H:%M")
    return r, s, {**log_r, **log_s}, loaded


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def metric(k: str, r: pd.DataFrame, s: pd.DataFrame):
    if k == "csat":
        c = s["csat"].dropna()
        return (c >= 4).mean() if len(c) else None
    if k in ("nps", "det"):
        n = s["nps"].dropna()
        if not len(n):
            return None
        return ((n >= 9).mean() - (n <= 6).mean()) * 100 if k == "nps" else (n <= 6).mean()
    stated = r[r["group"] != "Not stated"]
    if not len(stated):
        return None
    if k == "seller":
        return stated["group"].isin(SELLER_GROUPS).mean()
    if k == "fulfil":
        return (stated["group"] == "Fulfilment error").mean()
    if k == "size":
        return (stated["group"] == "Size & fit").mean()


def status(k, v):
    if v is None or pd.isna(v):
        return None
    t = KPI[k]
    if t["dir"] > 0:
        return "Excellent" if v >= t["ex"] else "Meets minimum" if v >= t["min"] else "Below minimum"
    return "Excellent" if v <= t["ex"] else "Meets minimum" if v <= t["min"] else "Below minimum"


def fmt(k, v):
    if v is None or pd.isna(v):
        return "n/a"
    return f"{v:.1%}" if KPI[k]["pct"] else f"{v:.1f}"


def target_txt(k, level):
    t = KPI[k]
    sign = "≥" if t["dir"] > 0 else "≤"
    return f"{sign} {t[level]:.0%}" if t["pct"] else f"{sign} {t[level]}"


def bucket(d: pd.Series, gran: str) -> pd.Series:
    ts = pd.to_datetime(d)
    if gran == "Day":
        return ts.dt.normalize()
    if gran == "Week":
        return (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.normalize()
    return ts.dt.to_period("M").dt.to_timestamp()


def series(k, df, gran):
    if df.empty:
        return pd.DataFrame(columns=["period", "value", "n"])
    rows = []
    for p, g in df.groupby(bucket(df["date"], gran)):
        r, s = (g, None) if KPI[k]["src"] == "returns" else (None, g)
        v = metric(k, r if r is not None else df.iloc[0:0], s if s is not None else df.iloc[0:0])
        rows.append({"period": p, "value": v, "n": len(g)})
    return pd.DataFrame(rows)


def share_table(cur: pd.Series, prev: pd.Series, label: str) -> pd.DataFrame:
    c = cur.value_counts()
    p = prev.value_counts()
    t = pd.DataFrame({label: c.index, "Count": c.values})
    t["Share"] = t["Count"] / max(c.sum(), 1)
    t["Previous share"] = t[label].map(p / p.sum() if p.sum() else {}).fillna(0)
    t["Change (pp)"] = ((t["Share"] - t["Previous share"]) * 100).round(1)
    return t


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_MINUTES * 60 * 1000, key="auto_refresh")
except ImportError:
    pass

st.title("Supersports Customer Experience: returns, NPS and CSAT")

try:
    R, S, LOG, LOADED = load_data()
except Exception as e:  # noqa: BLE001
    st.error(f"Could not load the Google Sheet: {e}")
    st.stop()

data_end = max(R["date"].max(), S["date"].max())
data_start = min(R["date"].min(), S[S["date"] >= date(2025, 1, 1)]["date"].min())

# ---------------- sidebar filters ----------------
with st.sidebar:
    st.header("Period")
    preset = st.radio(
        "Quick pick",
        ["Month to date", "Last month", "Last 7 days", "Last 30 days", "Last 90 days", "Quarter to date", "Year to date", "All data", "Custom"],
        index=0,
    )
    e = data_end
    if preset == "Month to date":
        a, b = e.replace(day=1), e
    elif preset == "Last month":
        b = e.replace(day=1) - timedelta(days=1); a = b.replace(day=1)
    elif preset.startswith("Last ") and preset.endswith("days"):
        n = int(preset.split()[1]); a, b = e - timedelta(days=n - 1), e
    elif preset == "Quarter to date":
        a, b = date(e.year, (e.month - 1) // 3 * 3 + 1, 1), e
    elif preset == "Year to date":
        a, b = date(e.year, 1, 1), e
    elif preset == "All data":
        a, b = data_start, e
    else:
        picked = st.date_input("From and to", (e.replace(day=1), e), min_value=date(2023, 1, 1), max_value=date.today())
        a, b = (picked if isinstance(picked, (list, tuple)) and len(picked) == 2 else (e.replace(day=1), e))
    ndays = (b - a).days + 1
    pb = a - timedelta(days=1); pa = pb - timedelta(days=ndays - 1)
    gran = "Day" if ndays <= 45 else "Week" if ndays <= 210 else "Month"
    st.caption(f"Selected: {a:%-d %b %Y} to {b:%-d %b %Y} ({ndays} days)")
    st.caption(f"Compared with: {pa:%-d %b %Y} to {pb:%-d %b %Y}")
    st.divider()
    st.caption(f"Data to: returns {R['date'].max():%-d %b %Y}, survey {S['date'].max():%-d %b %Y}")
    st.caption(f"Last read from Google Sheet: {LOADED} (auto every {REFRESH_MINUTES} min)")
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()

rc = R[(R["date"] >= a) & (R["date"] <= b)]
rp = R[(R["date"] >= pa) & (R["date"] <= pb)]
sc = S[(S["date"] >= a) & (S["date"] <= b)]
sp = S[(S["date"] >= pa) & (S["date"] <= pb)]
if len(rp) < 30:
    rp = R.iloc[0:0]
if len(sp) < 30:
    sp = S.iloc[0:0]

cur = {k: metric(k, rc, sc) for k in KPI}
prev = {k: metric(k, rp, sp) for k in KPI}
stat = {k: status(k, cur[k]) for k in KPI}
n_ex = sum(v == "Excellent" for v in stat.values())
n_bad = sum(v == "Below minimum" for v in stat.values())
st.markdown(f"**{a:%-d %b %Y} to {b:%-d %b %Y}:** {n_ex} of 6 core KPIs at Excellent, {n_bad} below the minimum.")

tab_over, tab_ret, tab_nps, tab_data = st.tabs(["Overview and KPIs", "Return reasons", "NPS and CSAT", "Definitions and data"])

# ---------------- Overview ----------------
with tab_over:
    st.subheader("Core KPIs")
    cols = st.columns(3)
    icon = {"Excellent": "🟢", "Meets minimum": "🟡", "Below minimum": "🔴", None: "⚪"}
    for i, k in enumerate(KPI):
        t = KPI[k]
        with cols[i % 3]:
            delta = None
            if cur[k] is not None and prev[k] is not None:
                d = cur[k] - prev[k]
                delta = f"{d * 100:+.1f} pp" if t["pct"] else f"{d:+.1f}"
            st.metric(f"{icon[stat[k]]} {t['name']}", fmt(k, cur[k]), delta,
                      delta_color="normal" if t["dir"] > 0 else "inverse")
            base = (f"{sc['csat'].notna().sum():,} ratings" if k == "csat" else f"{sc['nps'].notna().sum():,} answers"
                    if t["src"] == "survey" else f"{(rc['group'] != 'Not stated').sum():,} forms")
            st.caption(f"{stat[k] or 'No data'} · Excellent {target_txt(k, 'ex')}, minimum {target_txt(k, 'min')} · {base}")

    st.subheader("Volume in the period")
    v = st.columns(6)
    v[0].metric("Return forms", f"{len(rc):,}", f"{len(rc) / len(rp) - 1:+.1%}" if len(rp) else None, delta_color="inverse")
    v[1].metric("Return forms per day", f"{len(rc) / ndays:.1f}")
    v[2].metric("Repeat forms (same order)", f"{rc['repeat'].mean():.1%}" if len(rc) else "n/a")
    v[3].metric("Survey responses", f"{len(sc):,}", f"{len(sc) / len(sp) - 1:+.1%}" if len(sp) else None, delta_color="off")
    avg = sc["csat"].mean()
    v[4].metric("Average CSAT (1 to 5)", f"{avg:.2f}" if pd.notna(avg) else "n/a",
                f"{avg - sp['csat'].mean():+.2f}" if len(sp) and pd.notna(avg) else None)
    v[5].metric("Low scores to follow up", f"{int(sc['low'].sum()):,}")

    st.subheader("What to improve")
    th = pd.Series([t for ts in sc.loc[sc["low"], "themes"] for t in ts if t not in ("Other", "No reason given")]).value_counts()
    areas = pd.Series([x for xs in sc["areas"] for x in xs if x != "Nothing to improve"]).value_counts()
    seller_top = rc[rc["group"].isin(SELLER_GROUPS)]["reason"].value_counts().head(2)
    plat = sc.dropna(subset=["nps"]).groupby("platform")["nps"].agg(lambda n: ((n >= 9).mean() - (n <= 6).mean()) * 100)
    plat = plat[sc.dropna(subset=["nps"]).groupby("platform").size() >= 20].drop("Not stated", errors="ignore")
    tips = {
        "csat": (f"CSAT is {fmt('csat', cur['csat'])} against the {target_txt('csat', 'ex')} standard. Low scores mostly mention: "
                 + (", ".join(f"{n} ({c})" for n, c in th.head(3).items()) or "no clear theme") + ".",
                 ["Call back or LINE every CSAT 1 to 2 response within 24 hours, log the cause, offer a fix.",
                  "Give the top low score theme one owner and a weekly number (delivery: logistics, app/website: digital, refund: finance).",
                  "Read the low score comments in the NPS and CSAT tab in the weekly CX meeting."]),
        "nps": (f"NPS is {fmt('nps', cur['nps'])} against the {target_txt('nps', 'ex')} target. Weakest platform: "
                + (f"{plat.idxmin()} (NPS {plat.min():.1f})" if len(plat) else "not enough answers")
                + ". Most ticked improvement areas: " + (", ".join(areas.head(2).index) or "none") + ".",
                ["Close the loop with detractors: contact, solve, then ask again 30 days later.",
                 "Take the top two improvement areas to their owners with the specific requests in the NPS and CSAT tab.",
                 "Ask promoters (9 to 10) for a store or app review while they are happy."]),
        "det": (f"Detractors are {fmt('det', cur['det'])} of NPS answers ({int((sc['nps'] <= 6).sum()):,} customers).",
                ["Set a same day alert for every NPS 0 to 6 and route it to the CS queue as a case.",
                 "Check the platform table: a platform far below the others usually points to an app or website problem."]),
        "seller": (f"{fmt('seller', cur['seller'])} of returns are caused by us (website content, wrong item or size sent, quality). Biggest: "
                   + (", ".join(f"{n} ({c})" for n, c in seller_top.items()) or "none") + ".",
                   ["Weekly SKU list of 'looks different from website' and 'wrong measurement' to the content team: fix photos, colours, size charts.",
                    "Defective and quality returns by brand to merchandising for supplier claims; damaged parcels to logistics.",
                    "Ask for orders by SKU so return rate per product can be tracked."]),
        "fulfil": (f"Wrong size or wrong item sent is {fmt('fulfil', cur['fulfil'])} of returns ({int((rc['group'] == 'Fulfilment error').sum()):,} forms).",
                   ["Add a barcode scan check of size and SKU at packing; share each wrong pick with the warehouse weekly.",
                    "Pay return shipping and send the right item first for these customers: the error is ours."]),
        "size": (f"Size and fit drives {fmt('size', cur['size'])} of returns (too small {int((rc['reason'] == 'Item too small').sum()):,}, "
                 f"too big {int((rc['reason'] == 'Item too big').sum()):,}).",
                 ["Size chart in cm per brand and fit notes ('runs small, go half a size up') on every footwear and apparel page.",
                  "One size advice script for chat and livestream; ask foot length in cm before recommending.",
                  "Offer a size exchange (reorder link or voucher) before a refund in the return form."]),
    }
    order = sorted(KPI, key=lambda k: {"Below minimum": 0, "Meets minimum": 1}.get(stat[k], 2))
    shown = 0
    for k in order:
        if stat[k] in ("Below minimum", "Meets minimum"):
            shown += 1
            box = st.error if stat[k] == "Below minimum" else st.warning
            head, acts = tips[k]
            box(f"**{head}**\n\n" + "\n".join(f"- {x}" for x in acts))
    if len(rp) and len(rc) / len(rp) - 1 > 0.2:
        st.warning(f"**Return forms are up {len(rc) / len(rp) - 1:.1%} on the previous period.** Check whether sales also rose before treating it as a problem.")
    if len(rc) and rc["repeat"].mean() > 0.05:
        st.warning(f"**{rc['repeat'].mean():.1%} of forms repeat an order already submitted.** Send an automatic confirmation with next steps after every submission.")
    if not shown:
        st.success("All six core KPIs are at Excellent in this period.")

# ---------------- Returns ----------------
with tab_ret:
    st.subheader("Return forms by reason group")
    if rc.empty:
        st.info("No return forms in this period.")
    else:
        t = rc.assign(period=bucket(rc["date"], gran)).groupby(["period", "group"]).size().reset_index(name="forms")
        fig = px.bar(t, x="period", y="forms", color="group", category_orders={"group": GROUP_ORDER},
                     labels={"period": gran, "forms": "Forms", "group": "Reason group"})
        fig.update_layout(height=360, legend_orientation="h", margin=dict(t=10))
        st.plotly_chart(fig, width="stretch")

        st.subheader("Return reasons")
        rt = share_table(rc.loc[rc["group"] != "Not stated", "reason"], rp.loc[rp["group"] != "Not stated", "reason"], "Reason")
        rt.insert(1, "Group", rt["Reason"].map(dict(zip(R["reason"], R["group"]))))
        st.dataframe(rt, hide_index=True, width="stretch",
                     column_config={"Share": st.column_config.NumberColumn(format="percent"),
                                    "Previous share": st.column_config.NumberColumn(format="percent")})
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Return method")
            m = share_table(rc["method"], rp["method"], "Method")
            st.plotly_chart(px.bar(m, x="Share", y="Method", orientation="h", text_auto=".1%").update_layout(height=280, margin=dict(t=10), yaxis_title=None), width="stretch")
        with c2:
            st.subheader("Order details")
            st.dataframe(share_table(rc["scope"], rp["scope"], "Whole order or items"), hide_index=True, width="stretch", column_config={"Share": st.column_config.NumberColumn(format="percent"), "Previous share": st.column_config.NumberColumn(format="percent")})
            st.dataframe(share_table(rc["payment"], rp["payment"], "Payment method"), hide_index=True, width="stretch", column_config={"Share": st.column_config.NumberColumn(format="percent"), "Previous share": st.column_config.NumberColumn(format="percent")})
            st.caption(f"Items per form: {rc['items'].mean():.2f} (from {rc['items'].notna().sum():,} forms with a valid count)")

        st.subheader("Returns at stores (top 15)")
        stores = rc[rc["store"] != ""]
        if not stores.empty:
            g = stores.groupby("store").agg(Forms=("store", "size"),
                                            size_fit=("group", lambda x: (x == "Size & fit").mean()),
                                            caused_by_us=("group", lambda x: x.isin(SELLER_GROUPS).mean()))
            g["Previous"] = rp[rp["store"] != ""]["store"].value_counts().reindex(g.index).fillna(0).astype(int)
            g = g.sort_values("Forms", ascending=False).head(15).reset_index().rename(columns={"store": "Store", "size_fit": "Size and fit", "caused_by_us": "Caused by us"})
            st.dataframe(g, hide_index=True, width="stretch", column_config={"Size and fit": st.column_config.NumberColumn(format="percent"), "Caused by us": st.column_config.NumberColumn(format="percent")})
            st.caption("Branch is typed freely by customers and grouped by keyword; 'Other / unclear' is a store code, brand name or blank.")

        st.subheader("Return comments")
        q = st.text_input("Search comments", key="ret_q")
        com = rc[rc["comment"].str.len() > 3].sort_values("ts", ascending=False)
        if q:
            com = com[com["comment"].str.contains(q, case=False, regex=False)]
        st.caption(f"{len(com):,} comments (newest 300 shown; phone numbers and emails removed)")
        st.dataframe(com[["date", "reason", "method", "comment"]].head(300), hide_index=True, width="stretch")

# ---------------- NPS & CSAT ----------------
with tab_nps:
    if sc.empty:
        st.info("No survey responses in this period.")
    else:
        c1, c2 = st.columns(2)
        for col, k, color in ((c1, "csat", "#2E7D32"), (c2, "nps", "#1565C0")):
            with col:
                st.subheader("CSAT over time" if k == "csat" else "NPS over time")
                ser = series(k, sc, gran)
                fig = go.Figure(go.Scatter(x=ser["period"], y=ser["value"], mode="lines+markers", line=dict(color=color, width=2),
                                           customdata=ser["n"], hovertemplate="%{x|%d %b %Y}<br>%{y:" + (".1%" if KPI[k]["pct"] else ".1f") + "}<br>%{customdata} answers<extra></extra>"))
                fig.add_hline(y=KPI[k]["ex"], line_dash="dash", line_color="grey", annotation_text="Excellent")
                fig.add_hline(y=KPI[k]["min"], line_dash="dot", line_color="grey", annotation_text="Minimum")
                fig.update_layout(height=300, margin=dict(t=10), yaxis_tickformat=".0%" if KPI[k]["pct"] else None)
                st.plotly_chart(fig, width="stretch")
        c3, c4 = st.columns(2)
        with c3:
            st.subheader("CSAT score distribution")
            d = sc["csat"].dropna().astype(int).value_counts(normalize=True).reindex(range(1, 6), fill_value=0).reset_index()
            d.columns = ["Score", "Share"]
            d["Colour"] = d["Score"].map(lambda x: "4 to 5" if x >= 4 else "3" if x == 3 else "1 to 2")
            st.plotly_chart(px.bar(d, x="Score", y="Share", color="Colour", text_auto=".0%", color_discrete_map={"4 to 5": "#2E7D32", "3": "#9E9E9E", "1 to 2": "#C62828"}).update_layout(height=280, margin=dict(t=10), showlegend=False, yaxis_tickformat=".0%"), width="stretch")
        with c4:
            st.subheader("NPS score distribution")
            d = sc["nps"].dropna().astype(int).value_counts(normalize=True).reindex(range(0, 11), fill_value=0).reset_index()
            d.columns = ["Score", "Share"]
            d["Group"] = d["Score"].map(lambda x: "Promoter" if x >= 9 else "Passive" if x >= 7 else "Detractor")
            st.plotly_chart(px.bar(d, x="Score", y="Share", color="Group", text_auto=".0%", color_discrete_map={"Promoter": "#2E7D32", "Passive": "#9E9E9E", "Detractor": "#C62828"}).update_layout(height=280, margin=dict(t=10), yaxis_tickformat=".0%"), width="stretch")

        st.subheader("By platform and survey channel")
        rows = []
        for cut, colname in (("Platform", "platform"), ("Survey channel", "channel")):
            for name, g in sc.groupby(colname):
                n = g["nps"].dropna(); c = g["csat"].dropna()
                rows.append({"Cut": cut, "Group": name, "Responses": len(g), "CSAT": (c >= 4).mean() if len(c) else None,
                             "NPS": ((n >= 9).mean() - (n <= 6).mean()) * 100 if len(n) else None, "Detractors": (n <= 6).mean() if len(n) else None})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"CSAT": st.column_config.NumberColumn(format="percent"), "NPS": st.column_config.NumberColumn(format="%.1f"), "Detractors": st.column_config.NumberColumn(format="percent")})

        c5, c6 = st.columns(2)
        with c5:
            st.subheader("What customers want improved")
            ar = pd.Series([x for xs in sc["areas"] for x in xs]).value_counts()
            arp = pd.Series([x for xs in sp["areas"] for x in xs]).value_counts()
            at = pd.DataFrame({"Area": ar.index, "Share of respondents": ar.values / len(sc)})
            at["Change (pp)"] = ((at["Share of respondents"] - at["Area"].map(arp / max(len(sp), 1)).fillna(0)) * 100).round(1) if len(sp) else None
            st.plotly_chart(px.bar(at, x="Share of respondents", y="Area", orientation="h", text_auto=".1%", hover_data=["Change (pp)"]).update_layout(height=420, margin=dict(t=10), yaxis={"categoryorder": "total ascending", "title": None}, xaxis_tickformat=".0%"), width="stretch")
        with c6:
            st.subheader("Why customers gave a low score")
            low = sc[sc["low"]]
            tt = pd.Series([t for ts in low["themes"] for t in ts]).value_counts()
            tdf = pd.DataFrame({"Theme": tt.index, "Share of low scores": tt.values / max(len(low), 1)})
            st.plotly_chart(px.bar(tdf, x="Share of low scores", y="Theme", orientation="h", text_auto=".1%", color_discrete_sequence=["#C62828"]).update_layout(height=420, margin=dict(t=10), yaxis={"categoryorder": "total ascending", "title": None}, xaxis_tickformat=".0%"), width="stretch")
            st.caption("CSAT 1 to 3 or NPS 0 to 6; reason text grouped by keyword, one response can hit several themes.")

        st.subheader("Low score comments")
        f1, f2 = st.columns([1, 2])
        theme = f1.selectbox("Theme", ["All themes"] + list(tt.index))
        q2 = f2.text_input("Search comments", key="nps_q")
        lc = low.copy()
        if theme != "All themes":
            lc = lc[lc["themes"].map(lambda x: theme in x)]
        if q2:
            lc = lc[lc["text"].str.contains(q2, case=False, regex=False)]
        lc = lc.sort_values("ts", ascending=False)
        st.caption(f"{len(lc):,} comments (newest 300 shown; phone numbers and emails removed)")
        st.dataframe(lc.assign(Themes=lc["themes"].map(", ".join))[["date", "csat", "nps", "Themes", "text"]].head(300),
                     hide_index=True, width="stretch")

# ---------------- Definitions ----------------
with tab_data:
    st.subheader("KPI targets")
    st.dataframe(pd.DataFrame([{"KPI": t["name"], "Excellent": target_txt(k, "ex"), "Minimum": target_txt(k, "min"), "Basis": t["basis"]} for k, t in KPI.items()]),
                 hide_index=True, width="stretch")
    st.subheader("Definitions")
    st.markdown("""
- **CSAT**: ratings 4 or 5 divided by all valid 1 to 5 ratings. A 0 is not a valid score and is left out.
- **NPS**: % answers 9 to 10 minus % answers 0 to 6.
- **Returns caused by us**: website content (wrong measurement, looks different) + fulfilment error (wrong size or item received) + product quality (defective, quality, damaged parcel), as a share of forms with a reason.
- **Size and fit**: too small, too big, ordered several sizes.
- **Repeat form**: an order number that was already submitted in an earlier form.
- **Comparison**: the period of the same length just before (blank if it has fewer than 30 rows). Charts use days up to 45 days, weeks up to 210, then months.
- **Return rate** (returns / orders) is not shown: the sheet has no order data (TBC).
""")
    st.subheader("Data cleaning (this load)")
    st.dataframe(pd.DataFrame({"Step": list(LOG.keys()), "Rows": list(LOG.values())}), hide_index=True, width="stretch")
    st.caption(f"Source: Google Sheet {SHEET_ID}, tabs Return Form and NPS_CSAT. Bank, address, email, phone and name columns are not kept.")

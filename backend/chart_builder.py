import os
import matplotlib
import matplotlib.pyplot as plt
import datetime
from backend.database import run_query

# 伺服器端不顯示 GUI，使用 Agg backend
matplotlib.use('Agg')

# 配置支援中文字型，防止變成豆腐塊
plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei', 'SimHei', 'PingFang SC', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

# 確保圖片存檔目錄存在
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_DIR = os.path.join(BASE_DIR, "static_images")
os.makedirs(IMAGE_DIR, exist_ok=True)

def build_carbon_trend_chart() -> str:
    """產出碳排趨勢折線圖，回傳生成的圖片檔名"""
    q = '''
    SELECT strftime('%Y-%m', o.order_date) as month, SUM(COALESCE(o.quantity, 0) * COALESCE(cf.kg_co2_per_unit, 0)) as kg
    FROM orders o JOIN carbon_factors cf ON cf.product_id = o.product_id
    WHERE o.status != '已取消' AND o.order_date IS NOT NULL
    GROUP BY strftime('%Y-%m', o.order_date)
    ORDER BY month
    '''
    res = run_query(q)
    if not res:
        return None

    months = [r[0] for r in res if r[0] is not None]
    kgs = [r[1] for r in res if r[0] is not None]
    
    if not months:
        return None

    plt.figure(figsize=(8, 5))
    plt.plot(months, kgs, marker='o', linestyle='-', color='#2E8B57', linewidth=2, markersize=8)
    plt.fill_between(months, kgs, color='#2E8B57', alpha=0.2)
    
    plt.title('歷月碳排放(Scope 1~3) 趨勢分析', fontsize=16, fontweight='bold', pad=15)
    plt.xlabel('年月', fontsize=12)
    plt.ylabel('碳排放量 (kg CO2e)', fontsize=12)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    # 稍微旋轉 X 軸標籤避免重疊
    plt.xticks(rotation=45)
    plt.tight_layout()

    filename = f"carbon_trend_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.png"
    filepath = os.path.join(IMAGE_DIR, filename)
    plt.savefig(filepath, dpi=120)
    plt.close()
    
    return filename


def build_finance_pie_chart() -> str:
    """產出財務收支佔比圓餅圖，回傳生成的圖片檔名"""
    
    # 取得庫存總額
    res_inv = run_query("SELECT SUM(stock * COALESCE(cost, 0)) FROM inventory")
    inv_val = float(res_inv[0][0]) if res_inv and res_inv[0][0] else 0

    # 取得應收帳款餘額
    res_rec = run_query("SELECT SUM(amount - COALESCE(paid, 0)) FROM receivables")
    rec_val = float(res_rec[0][0]) if res_rec and res_rec[0][0] else 0

    # 取得應付帳款餘額
    res_pay = run_query("SELECT SUM(amount - COALESCE(paid, 0)) FROM payables")
    pay_val = float(res_pay[0][0]) if res_pay and res_pay[0][0] else 0

    labels = []
    sizes = []
    colors = ['#4CAF50', '#2196F3', '#F44336'] # 綠(庫存), 藍(應收), 紅(應付)
    
    if inv_val > 0:
        labels.append('庫存總資產')
        sizes.append(inv_val)
    if rec_val > 0:
        labels.append('應收帳款餘額(資產)')
        sizes.append(rec_val)
    if pay_val > 0:
        labels.append('應付帳款餘額(負債)')
        sizes.append(pay_val)

    if sum(sizes) == 0:
        return None

    plt.figure(figsize=(7, 6))
    patches, texts, autotexts = plt.pie(
        sizes, 
        labels=labels, 
        colors=colors[:len(sizes)], 
        autopct='%1.1f%%', 
        startangle=140,
        wedgeprops={'edgecolor': 'white', 'linewidth': 2}
    )
    
    # 字體調整
    for text in texts:
        text.set_fontsize(12)
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontsize(12)
        autotext.set_fontweight('bold')

    plt.title('目前資產與負債比例分析', fontsize=16, fontweight='bold', pad=20)
    plt.tight_layout()

    filename = f"finance_pie_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.png"
    filepath = os.path.join(IMAGE_DIR, filename)
    plt.savefig(filepath, dpi=120)
    plt.close()

    return filename

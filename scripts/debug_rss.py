
import re
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request

def test_rss(country_name, within_days=90):
    name_en = country_name
    # Current query format
    query = f"{name_en} (supply chain OR logistics OR shipping OR export OR shortage OR conflict OR disruption OR port OR strike) when:{within_days}d"
    q_enc = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q_enc}&hl=en-US&gl=US&ceid=US:en"
    print(f"Testing URL: {url}")
    
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ERP-Bot/1.0)"})
        with urlopen(req, timeout=15) as resp:
            content = resp.read()
            print(f"Content length: {len(content)}")
            tree = ET.fromstring(content)
        
        channel = tree.find("channel")
        items = channel.findall("item")
        print(f"Found {len(items)} items")
        for it in items[:3]:
            print(f" - {it.find('title').text}")
            
    except Exception as e:
        print(f"Error: {e}")

print("--- Testing Taiwan ---")
test_rss("Taiwan")
print("\n--- Testing Japan ---")
test_rss("Japan")

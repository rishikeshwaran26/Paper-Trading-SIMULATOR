import urllib.request
import re

url = 'https://www.amfiindia.com/research-information/other-data'
try:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        html = response.read().decode('utf-8')
        links = re.findall(r'href=[\'\"]([^\'\"]+)[\'\"]', html)
        for link in links:
            if 'categorization' in link.lower() or 'excel' in link.lower() or '.xls' in link.lower():
                print(link)
except Exception as e:
    print('Error on main:', e)

url2 = 'https://www.amfiindia.com/Themes/Theme1/downloads/categorization-of-stocks'
try:
    req = urllib.request.Request(url2, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        html = response.read().decode('utf-8')
        links = re.findall(r'href=[\'\"]([^\'\"]+)[\'\"]', html)
        for link in links:
            if '.xls' in link.lower():
                print(link)
except Exception as e:
    print('Error on url2:', e)

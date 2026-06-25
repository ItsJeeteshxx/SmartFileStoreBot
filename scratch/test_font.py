import urllib.request

urls = [
    "https://github.com/google/fonts/raw/main/ofl/roboto/static/Roboto-Regular.ttf",
    "https://github.com/google/fonts/raw/main/ofl/roboto/Roboto-Regular.ttf",
    "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Regular.ttf",
    "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Regular.ttf"
]

for url in urls:
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req) as resp:
            print(f"URL: {url} -> STATUS: {resp.status}")
    except Exception as e:
        print(f"URL: {url} -> ERROR: {e}")

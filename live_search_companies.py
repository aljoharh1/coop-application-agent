import re, time, pandas as pd
from duckduckgo_search import DDGS

queries = [
    'site:linkedin.com/jobs "COOP" OR "Cooperative Training" "Riyadh" "AI" OR "Data" 2026 2027',
    '"tdribp taawuni" OR "تدريب تعاوني" "ذكاء اصطناعي" OR "علم البيانات" "الرياض" 2027',
    '"COOP" "Artificial Intelligence" OR "Data Science" "Riyadh" apply 2027',
    '"FinTech" OR "Cybersecurity" company "North Riyadh" OR "KAFD" OR "Al Yasmin" OR "Al Sahafah" careers',
    '"AI startup" OR "Data Analytics" company "Riyadh" careers',
]

HIGH_GPA = re.compile(r'(3\.[6-9]\d*|4\.[0-9]\d*|2\.[8-9]\d*\s*/\s*4|3\.[0-9]\d*\s*/\s*4|very good|excellent|جيد\s*جدا~جيد+\s*جدا׋ممتز)', re.I)
CLOSED = re.compile(r'(closed|expired|no longer accepting|انتهى التقديم}مغلق)', re.I)

results_map = {}
with DDGS() as ddgs:
    for q in queries:
        print('Searching:', q)
        try:
            for r in ddgs.text(q, region='sa-ar', max_results=40):
                url, title, body = r.get('href',''), r.get('title',''), r.get('body','')
                txt = f'{title} {body}'
                if not url or url in results_map or HIGH_GPA.search(txt) or CLOSED.search(txt):
                    continue
                score = 50
                low = txt.lower()
                if any(k in low for k in ['kafd','yasmin','sahafah','malqa','aqiq','nakheel','riyadh front']): score += 20
                if any(k in low for k in ['ai','artificial intelligence','data','machine learning','nlp','cyber','fintech']): score += 20
                if any(k in low for k in ['coop','cooperative','intern','2027']): score += 10
                results_map[url] = {'Fit Score': min(score,100), 'Title / Company': title, 'Live Link': url, 'Snippet': body}
            time.sleep(2)
        except Exception as e:
            print('Warning:', e)

df = pd.DataFrame(list(results_map.values()))
if not df.empty:
    df = df.sort_values(by='Fit Score', ascending=False).reset_index(drop=True)
    df.insert(0, 'Rank', df.index + 1)
    df.to_excel('Riyadh_Live_AI_Opportunities.xlsx', index=False)
    print(f'Saved {len(df)} results to Riyadh_Live_AI_Opportunities.xlsx')
    for _, row in df.head(15).iterrows():
        print(f"# {row['Rank']} [{row['Fit Score']}] {row['Title / Company']}\n   {row['Live Link']}")
else:
    print('No results returned')

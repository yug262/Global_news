import json
from db import fetch_all, execute_query
from agent import create_predictions

def backfill():
    articles = fetch_all("SELECT id, analysis_data FROM news WHERE analyzed = TRUE AND analysis_data IS NOT NULL")
    count = 0
    for article in articles:
        try:
            # Check if predictions already exist
            preds = fetch_all("SELECT id FROM predictions WHERE news_id = %s", (article["id"],))
            if len(preds) > 0:
                continue

            analysis = article["analysis_data"]
            if isinstance(analysis, str):
                analysis = json.loads(analysis)

            create_predictions(article["id"], analysis)
            count += 1
        except Exception as e:
            print(f"Failed for {article['id']}: {e}")

    print(f"Backfilled {count} articles.")

if __name__ == "__main__":
    backfill()

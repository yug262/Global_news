"""
Delete all backfilled predictions (those that used bad historical start times)
and reset them so that re-analyzing the news articles will recreate them
accurately with the current real price as start.
"""
from db import execute_query, fetch_all
import json
from agent import create_predictions

def reset():
    print("Deleting all old pending predictions...")
    execute_query("DELETE FROM predictions")
    print("Done. All predictions removed.")

    print("\nNow recreating predictions using current live prices...")
    articles = fetch_all("SELECT id, analysis_data FROM news WHERE analyzed = TRUE AND analysis_data IS NOT NULL")
    count = 0
    for article in articles:
        try:
            analysis = article["analysis_data"]
            if isinstance(analysis, str):
                analysis = json.loads(analysis)
            create_predictions(article["id"], analysis)
            count += 1
        except Exception as e:
            print(f"Error for news {article['id']}: {e}")

    print(f"Recreated predictions for {count} articles with current prices as the baseline.")
    print("The start_time has been pinned to right now so the monitor tracks from this exact moment forward.")

if __name__ == "__main__":
    reset()

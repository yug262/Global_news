import asyncio
from dotenv import load_dotenv

load_dotenv()
from app.core.india_agent import analyze_indian_news

async def main():
    title = "RBI hikes repo rate by 25 bps to tackle inflation"
    desc = "The Reserve Bank of India has unexpectedly increased the benchmark lending rate by 25 basis points in a move to curb rising inflation ahead of the festival season."
    
    print("Testing analyze_indian_news...")
    result = await analyze_indian_news(title, desc)
    print("Result:", result)

if __name__ == "__main__":
    asyncio.run(main())

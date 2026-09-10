from openai import OpenAI
from app.config import settings

client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                timeout=settings.model_timeout_seconds, max_retries=0)

def embed(texts: list[str]) -> list[list[float]]:
    return [x.embedding for x in client.embeddings.create(model=settings.embedding_model, input=texts).data]

def answer(question: str, contexts: list[str]) -> str:
    prompt = "Answer only from the supplied context. If absent, say you don't know.\n\nContext:\n" + "\n---\n".join(contexts) + "\n\nQuestion: " + question
    return client.chat.completions.create(model=settings.chat_model, messages=[{"role": "user", "content": prompt}]).choices[0].message.content or ""

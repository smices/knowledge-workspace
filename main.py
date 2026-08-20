from app.main import app


@app.get("/")
def root():
    return {"status": "ok"}

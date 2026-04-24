from fastapi import FastAPI

app = FastAPI(title="{{display_name}}")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "unit_id": "{{unit_id}}"}


@app.get("/")
def root() -> dict:
    return {
        "unit_id": "{{unit_id}}",
        "display_name": "{{display_name}}",
        "message": "Managed python service is running.",
    }


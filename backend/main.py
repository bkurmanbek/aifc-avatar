from .api.app import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    from .settings import APP_HOST, APP_PORT

    uvicorn.run("backend.main:app", host=APP_HOST, port=APP_PORT, reload=False)

"""容器入口：python run.py 直接启动。"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0",
                port=int(os.environ.get("SP_PORT", "47310")),
                log_level="warning")

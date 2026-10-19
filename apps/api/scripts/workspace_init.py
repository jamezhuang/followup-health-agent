"""Create only cb_private_* / cb_workspace_* tables; never rebuild public RAG."""
import os
from app.config import Settings
from app.workspace.repository import Repository


def main():
    settings = Settings()
    repo = Repository(os.getenv("RAG_DATABASE_URL", ""), settings.dimensions, settings.embedding_model)
    try:
        repo.initialize()
        print("个人资料与运行记录表已创建。公共知识索引未改动。")
    finally:
        repo.engine.dispose()


if __name__ == "__main__":
    main()

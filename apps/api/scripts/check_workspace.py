"""pgvector smoke test using synthetic data. No LLM/Embedding API requests."""
import json
import os
from uuid import uuid4

from app.config import Settings
from app.workspace.repository import Repository


def main():
    settings=Settings()
    repo=Repository(os.getenv("RAG_DATABASE_URL",""),settings.dimensions,settings.embedding_model)
    repo.initialize()
    owner="workspace-smoke-"+uuid4().hex
    ids=[]
    try:
        for patient,value in [(owner,"6.0"),(owner+"-other","99.0")]:
            doc,_=repo.create_document(patient,"synthetic-smoke.txt",".txt","synthetic-no-file.txt",uuid4().hex,8)
            ids.append((patient,doc["id"]))
            pieces=[{"location":"合成段落 1","text":"synthetic WBC = "+value}]
            repo.transition(patient,doc["id"],1,["queued"],"awaiting_confirmation",pieces=pieces)
            revision=repo.confirm(patient,doc["id"],1,pieces)
            vector=[1.0]+[0.0]*(settings.dimensions-1)
            repo.save_chunks(patient,doc["id"],revision,[{"text":pieces[0]["text"],"location":pieces[0]["location"]}],[vector])
        result=repo.search(owner,"synthetic WBC",[1.0]+[0.0]*(settings.dimensions-1),3)
        assert result and all(r["document_id"]==ids[0][1] for r in result)
        repo.delete_document(owner,ids[0][1])
        assert repo.search(owner,"synthetic WBC",[1.0]+[0.0]*(settings.dimensions-1),3)==[]
        print(json.dumps({"postgres_pgvector":True,"owner_filter":True,"delete_exclusion":True,"provider_requests":0}))
    finally:
        for patient,doc_id in ids:
            try:repo.delete_document(patient,doc_id)
            except Exception:pass
        repo.engine.dispose()


if __name__=="__main__":main()

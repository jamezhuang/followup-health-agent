"""Material confirmation is a server fact, independent of model wording."""
NOTICE='本回答包含自动识别且未经核对的报告信息，请对照原报告核对；这些未核对内容尚未确认归档。'


def mark_material_status(result, documents):
    pending=[document['id'] for document in documents if not document.get('confirmed')]
    if not pending:
        return result
    result['material_status']={'has_unconfirmed_reports':True,'document_ids':pending}
    result['material_notice']=NOTICE
    if result.get('kind')=='chat' and not result.get('content','').startswith(NOTICE):
        result['content']=NOTICE+'\n\n'+result.get('content','')
    return result

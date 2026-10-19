"""Export only workflow operations, with their referenced schemas; no credentials."""
import argparse,json
from pathlib import Path
from urllib.request import urlopen

def workflow_schema(schema,server):
    result={'openapi':schema['openapi'],'info':{'title':'CareBrief Workflow','version':'1.0.0'},
            'servers':[{'url':server.rstrip('/')}],
            'paths':{p:v for p,v in schema['paths'].items() if p.startswith('/v2/integrations/workflow/')},
            'components':{'schemas':{},'securitySchemes':{'CareBriefWorkflowKey':schema['components']['securitySchemes']['CareBriefWorkflowKey']}}}
    pending=[result['paths']]
    while pending:
        value=pending.pop()
        if isinstance(value,dict):
            ref=value.get('$ref','')
            if ref.startswith('#/components/schemas/'):
                name=ref.rsplit('/',1)[1]
                if name not in result['components']['schemas']:
                    component=schema['components']['schemas'][name]
                    result['components']['schemas'][name]=component;pending.append(component)
            pending.extend(value.values())
        elif isinstance(value,list):pending.extend(value)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:8002');parser.add_argument('--output',default='docs/workflow-openapi.json')
    args=parser.parse_args()
    with urlopen(args.base_url.rstrip('/')+'/openapi.json',timeout=10) as response:schema=json.load(response)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(workflow_schema(schema,args.base_url),ensure_ascii=False,indent=2),encoding='utf-8')
    print(str(path))

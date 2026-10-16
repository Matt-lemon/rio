"""Platform-owned pipeline contract. Callers cannot replace this profile."""
from core.tools.contracts import digest

def pipeline(image_id):
    contract={'name':'pipeline_run','description':'Bounded allowlisted text pipeline in Docker',
              'version':'1.0.0','kind':'pipeline','image_id':image_id,
              'permissions':['input.read','workspace.compute','workspace.output'],
              'input_schema':{'type':'object','properties':{
                  'text':{'type':'string','maxLength':1048576},
                  'steps':{'type':'array','minItems':1,'maxItems':4,'items':{
                      'type':'object','properties':{'command':{'enum':['sort','uniq','wc']},
                          'args':{'type':'array','maxItems':3,'items':{'type':'string'}}},
                      'required':['command','args'],'additionalProperties':False}}},
                  'required':['text','steps'],'additionalProperties':False},
              'output_schema':{'type':'object','properties':{'text':{'type':'string','maxLength':1048576}},
                               'required':['text'],'additionalProperties':False}}
    return {'name':'pipeline_run','hash':digest(contract),'version':'1.0.0','state':'ACTIVE',
            'contract':contract}

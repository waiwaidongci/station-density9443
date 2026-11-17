from pydantic import BaseModel,ConfigDict
class PurgeInput(BaseModel):
    model_config=ConfigDict(extra='ignore')
    source_id:int|None=None
    source:int|None=None
    references:list[str]|str|None=None
    reference:str|None=None
    record_references:list[str]|str|None=None

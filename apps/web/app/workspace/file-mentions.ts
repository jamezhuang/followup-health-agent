export type SelectedFileName = {id:string; name:string};
type FileMention = {id:string; name:string; start:number; end:number};
export type FileMentionDraft = {text:string; mentions:FileMention[]};

export function emptyFileMentionDraft(text=""):FileMentionDraft {
  return {text,mentions:[]};
}

// Keep ownership only for unchanged auto-inserted ranges. Never remove all
// occurrences of a filename: users may mention that name in their own prompt.
export function editFileMentionDraft(draft:FileMentionDraft,text:string):FileMentionDraft {
  if(text===draft.text)return draft;
  let start=0;
  while(start<draft.text.length && start<text.length && draft.text[start]===text[start])start++;
  let suffix=0;
  while(suffix<draft.text.length-start && suffix<text.length-start &&
    draft.text[draft.text.length-1-suffix]===text[text.length-1-suffix])suffix++;
  const end=draft.text.length-suffix;
  const delta=text.length-draft.text.length;
  const mentions=draft.mentions.flatMap(mention=>{
    if(mention.end<=start)return [mention];
    if(mention.start>=end)return [{...mention,start:mention.start+delta,end:mention.end+delta}];
    return [];
  });
  return {text,mentions};
}

export function syncFileMentionDraft(
  draft:FileMentionDraft,
  previous:SelectedFileName[],
  selected:SelectedFileName[],
  maxLength=2000,
):{draft:FileMentionDraft; skippedNames:string[]; added:boolean} {
  let result=draft;
  const current=new Map(selected.map(file=>[file.id,file.name]));
  const prior=new Map(previous.map(file=>[file.id,file.name]));
  const removed=draft.mentions.filter(mention=>current.get(mention.id)!==mention.name)
    .sort((a,b)=>b.start-a.start);
  for(const mention of removed){
    if(result.text.slice(mention.start,mention.end)!==mention.name)continue;
    // Remove just the generated name and its one separating space.
    const end=mention.end+(result.text[mention.end]===" "?1:0);
    result=editFileMentionDraft(result,result.text.slice(0,mention.start)+result.text.slice(end));
  }
  result={...result,mentions:result.mentions.filter(mention=>current.get(mention.id)===mention.name)};
  let prefix="";
  const inserted:FileMention[]=[];
  const skippedNames:string[]=[];
  for(const file of selected){
    if(!file.name || prior.get(file.id)===file.name)continue;
    if(prefix.length+file.name.length+1+result.text.length>maxLength){skippedNames.push(file.name);continue;}
    inserted.push({id:file.id,name:file.name,start:prefix.length,end:prefix.length+file.name.length});
    prefix+=file.name+" ";
  }
  if(prefix){
    result={text:prefix+result.text,mentions:[...inserted,...result.mentions.map(mention=>({
      ...mention,start:mention.start+prefix.length,end:mention.end+prefix.length,
    }))]};
  }
  return {draft:result,skippedNames,added:inserted.length>0};
}

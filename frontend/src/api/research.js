import {api} from './client.js';
export const sourcesApi={list:()=>api('/sources'),create:data=>api('/sources',{method:'POST',body:JSON.stringify(data)}),update:(id,data)=>api('/sources/'+id,{method:'PUT',body:JSON.stringify(data)}),records:id=>api('/sources/'+id+'/records'),add:(id,records)=>api('/sources/'+id+'/records',{method:'POST',body:JSON.stringify({records})})};
export const runsApi={list:()=>api('/runs'),get:id=>api('/runs/'+id),create:data=>api('/runs',{method:'POST',body:JSON.stringify(data)})};
export const notesApi={list:()=>api('/notes'),create:data=>api('/notes',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/notes/'+id+'/approve',{method:'POST'})};
export const purgeApi={list:()=>api('/purge-requests'),get:id=>api('/purge-requests/'+id),create:(sourceId,references)=>api('/sources/'+sourceId+'/purge',{method:'POST',body:JSON.stringify({references})}),retry:id=>api('/purge-requests/'+id+'/retry',{method:'POST'})};

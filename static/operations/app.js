/* Presentation enhancements only; all mutations and financial authority live on the server. */
document.querySelectorAll('.sidebar nav a').forEach(link=>{if(new URL(link.href).pathname===location.pathname)link.setAttribute('aria-current','page')});
const errors=document.querySelector('.error-summary');if(errors)errors.focus();
// A status page may opt in with its same-origin status URL and resource identity.
let generation=0;let stopped=false;let controller;let version=-1;
const statusNode=document.querySelector('[data-operation-status]');
async function poll(){if(!statusNode||stopped)return;const own=++generation;controller=new AbortController();try{const response=await fetch(statusNode.dataset.operationStatus,{credentials:'same-origin',cache:'no-store',signal:controller.signal});if(response.status===401||response.redirected){stopped=true;return}if(!response.ok)return;const data=await response.json();if(stopped||own!==generation||data.operation_id!==statusNode.dataset.operationId||data.version<version)return;version=data.version;statusNode.textContent=data.safe_message;if(['SUCCEEDED','FAILED','CANCELLED'].includes(data.state)){stopped=true;return}}catch(error){if(error.name!=='AbortError')statusNode.textContent='Status is temporarily unavailable. Refresh this page to inspect the saved operation.'}finally{if(!stopped)setTimeout(poll,3000)}}
window.addEventListener('pagehide',()=>{stopped=true;generation++;controller?.abort()});if(statusNode)poll();

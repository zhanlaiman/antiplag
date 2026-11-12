'use strict';
const message=document.querySelector('#owner-message');
async function ownerRequest(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'Не удалось выполнить вход');return data;}
document.querySelector('#owner-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;try{const fields=new FormData(event.target);await ownerRequest('/api/owner/login',{username:fields.get('username'),password:fields.get('password')});event.target.reset();location.assign('/#check');}catch(error){message.textContent=error.message;}finally{button.disabled=false;}});
document.querySelector('#owner-logout').addEventListener('click',async()=>{try{await ownerRequest('/api/owner/logout',{});location.assign('/');}catch(error){message.textContent=error.message;}});

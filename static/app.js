// Browser client for the server's Socket.IO event contract.
// The backend owns the authoritative board and all legality checks; this file
// renders per-player snapshots and sends user intent rather than deciding rules.
const socket = io();
const $ = id => document.getElementById(id);
let mySide = null, roomCode = null, state = null, selected = null, handledRequestId = null, activeIncomingRequest = null;
let toastTimer;
function toast(message){$('toast').textContent=message;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),2600)}
socket.on('connect',()=>$('connection').textContent='已连接');
socket.on('disconnect',()=>$('connection').textContent='连接断开');
socket.on('notice',d=>toast(d.message));
socket.on('nickname_ok',d=>{localStorage.setItem('darkxiangqiNickname',d.nickname);$('login-panel').classList.add('hidden');$('my-name').textContent=d.nickname;if(roomCode){$('lobby').classList.add('hidden');$('game').classList.remove('hidden')}else{$('lobby').classList.remove('hidden')}toast('昵称已保存到服务器')});
socket.on('room_joined',d=>{roomCode=d.code;mySide=d.side;$('game').classList.remove('hidden');$('code-label').textContent=d.code;$('side-label').textContent=d.side==='red'?'红方':'黑方';$('lobby').classList.add('hidden')});
socket.on('state',s=>{state=s;roomCode=s.code;mySide=s.side;$('game').classList.remove('hidden');$('login-panel').classList.add('hidden');$('lobby').classList.add('hidden');render();handleIncomingRequest()});
socket.on('room_left',()=>{closeRequestDialog();roomCode=null;mySide=null;state=null;selected=null;handledRequestId=null;$('game').classList.add('hidden');$('lobby').classList.remove('hidden');toast('已退出房间')});
socket.on('room_ended',d=>{closeRequestDialog();const message=d.winner==='draw'?'本局结束：双方和棋。':d.winner===mySide?'本局结束：你赢了！':'本局结束：对方获胜。';roomCode=null;mySide=null;state=null;selected=null;handledRequestId=null;$('game').classList.add('hidden');$('lobby').classList.remove('hidden');toast(message)});
// The HTTP nickname request can create/update the signed session cookie.
// Reconnect Socket.IO so its handshake uses that same session and stable player ID.
function reconnectWithNickname(nickname){
 // /api/nickname may have just issued a new signed session cookie. Reconnect so
 // the Socket.IO handshake sees the same persistent player_id as HTTP requests.
 socket.once('connect',()=>socket.emit('set_nickname',{nickname}));
 if(socket.connected)socket.disconnect();
 socket.connect();
}
$('save-name').onclick=async()=>{try{const r=await fetch('/api/nickname',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nickname:$('nickname').value})});const d=await r.json();if(!r.ok){toast(d.error||'昵称无效');return}reconnectWithNickname(d.nickname)}catch{toast('无法保存昵称，请检查服务器连接')}};
$('create-room').onclick=()=>socket.emit('create_room');
$('join-room').onclick=()=>socket.emit('join_room',{code:$('room-code').value});
$('room-code').addEventListener('keydown',e=>{if(e.key==='Enter')$('join-room').click()});
$('nickname').addEventListener('keydown',e=>{if(e.key==='Enter')$('save-name').click()});
$('copy-code').onclick=async()=>{try{await navigator.clipboard.writeText(roomCode);toast('房间号已复制')}catch{toast('房间号：'+roomCode)}};
$('request-undo').onclick=()=>socket.emit('request_undo');
$('request-draw').onclick=()=>socket.emit('request_draw');
$('surrender').onclick=()=>{if(confirm('确定投降吗？投降后本局立即结束。'))socket.emit('surrender')};
socket.on('request_result',d=>toast(d.message));
// A pending request is included in each state snapshot. The receiver's client
// turns it into an in-page alert dialog; request ID validation stays server-side.
function handleIncomingRequest(){
 const req=state&&state.pending_request;
 if(!req){closeRequestDialog();handledRequestId=null;return}
 if(req.to_side!==mySide)return;
 if(handledRequestId===req.id)return;
 handledRequestId=req.id;
 activeIncomingRequest={id:req.id,type:req.type};
 const requester=(state.players&&state.players[req.from_side])||'对方';
 const message=req.type==='undo'
  ?`${requester}申请悔棋。是否同意撤销双方各自最近的一步行棋（共两步）？`
  :`${requester}申请和棋。是否同意结束本局并判为平局？`;
 $('request-title').textContent=req.type==='undo'?'悔棋申请':'和棋申请';
 $('request-message').textContent=message;
 $('request-dialog').classList.remove('hidden');
 $('accept-request').focus();
}
function closeRequestDialog(){
 $('request-dialog').classList.add('hidden');
 activeIncomingRequest=null;
}
function respondToIncomingRequest(accepted){
 if(!activeIncomingRequest)return;
 const req=activeIncomingRequest;
 closeRequestDialog();
 socket.emit('respond_request',{id:req.id,accepted});
}
$('accept-request').onclick=()=>respondToIncomingRequest(true);
$('reject-request').onclick=()=>respondToIncomingRequest(false);

// Internal piece codes are translated to Chinese glyphs only for display.
const glyph = {K:'帅',A:'仕',B:'相',N:'马',R:'车',C:'炮',P:'兵',k:'将',a:'士',b:'象',n:'马',r:'车',c:'炮',p:'卒'};
// Rebuild the 9x10 grid from the server snapshot. Hidden types never arrive
// in state.board, so the browser cannot infer the opponent's hidden pieces here.
function render(){
 if(!state)return;
 $('turn-label').textContent=state.winner?(state.winner==='draw'?'双方和棋':state.winner===mySide?'你赢了':'对方获胜'):!state.started?'等待玩家加入…':state.turn==='red'?'红方走棋':'黑方走棋';
 $('red-player').textContent=state.players.red||'等待加入';$('black-player').textContent=state.players.black||'等待加入';
 $('status').textContent=state.winner?(state.winner==='draw'?'本局结束，双方和棋。':state.winner===mySide?'恭喜，你赢了！':'本局结束，你输了。'):!state.started?'把房间号发给另一位玩家。':state.pending_request?'有申请正在等待处理。':state.turn===mySide?'轮到你了，选择棋子再点击目标位置。':'等待对方落子。';
 $('request-undo').disabled=!state.started||!!state.winner||state.turn!==mySide||!state.can_undo||!!state.pending_request||!!(state.request_locks&&state.request_locks.undo);
 $('request-draw').disabled=!state.started||!!state.winner||state.turn!==mySide||!!state.pending_request||!!(state.request_locks&&state.request_locks.draw);
 $('surrender').disabled=!state.started||!!state.winner;
 const board=$('board');board.innerHTML='';
 for(let y=0;y<10;y++)for(let x=0;x<9;x++){
  const cell=document.createElement('button');cell.type='button';cell.className='cell';cell.setAttribute('aria-label',`第${y+1}行第${x+1}列`);
  const p=state.board[y][x];
  if(selected&&selected.y===y&&selected.x===x)cell.classList.add('selected');
  const lastMove=state.last_move;if(lastMove&&((lastMove.from.y===y&&lastMove.from.x===x)||(lastMove.to.y===y&&lastMove.to.x===x)))cell.classList.add('last-move');
  if(p){const span=document.createElement('span');span.className='piece '+p.side+(p.face_up?'':' hidden-piece');span.textContent=p.face_up?glyph[p.kind]:'？';cell.appendChild(span)}
  cell.onclick=()=>clickCell(y,x);board.appendChild(cell);
 }
 renderCaptured('red');renderCaptured('black');
 const log=$('log');log.innerHTML='';state.log.slice().reverse().forEach(line=>{const li=document.createElement('li');li.textContent=line;log.appendChild(li)});
}
// Captured-piece visibility was filtered per viewer by the server snapshot.
function renderCaptured(side){
 const el=$('captured-'+side);el.innerHTML='';
 (state.captured[side]||[]).forEach(p=>{const s=document.createElement('span');s.className='captured-piece '+p.side;s.textContent=p.kind?glyph[p.kind]:'暗子';s.title=p.kind?'已知棋子种类':'棋种未知';el.appendChild(s)});
}
// Selection is a UI affordance only. Every attempted move is revalidated by app.py.
function clickCell(y,x){
 if(!state||!state.started||state.winner)return;
 const p=state.board[y][x];
 if(!selected){
  if(!p){toast('请选择一个己方棋子');return}
  if(p.side!==mySide){toast('不能选择对方棋子');return}
  if(state.turn!==mySide){toast('还没轮到你');return}
  selected={y,x};render();return;
 }
 if(selected.y===y&&selected.x===x){selected=null;render();return}
 if(p&&p.side===mySide){selected={y,x};render();return}
 const from=selected;selected=null;
 socket.emit('move',{from,to:{y,x}});render();
}
// Rehydrate the nickname field on load. Socket.IO connect restoration then
// rejoins any saved match associated with this browser's existing session cookie.
fetch('/api/me').then(r=>r.json()).then(d=>{if(d.nickname){$('nickname').value=d.nickname;socket.emit('set_nickname',{nickname:d.nickname})}else{const saved=localStorage.getItem('darkxiangqiNickname');if(saved)$('nickname').value=saved}}).catch(()=>{});

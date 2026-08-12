(function(){
const React=window.React,{useEffect,useMemo,useState}=React,h=React.createElement;
const API='/hunterbot/admin-console/api';
const labels={'AI对话':'AI 对话','消息采集':'消息采集','日报':'群日报','自动日报':'自动发送日报','陪伴画像':'智能陪伴','常数报时':'常数报时','调戏其他bot':'调戏其他 Bot','常数回怼':'常数回怼','关键词回怼':'关键词回怼'};
async function request(path,options){const r=await fetch(API+path,{headers:{'Content-Type':'application/json'},...options});if(!r.ok){let d={};try{d=await r.json()}catch(e){}throw new Error(d.detail||('请求失败 '+r.status))}return r.json()}
function Button({children,onClick,kind='',disabled=false}){return h('button',{className:'btn '+kind,onClick,disabled},children)}
function Card({children,className=''}){return h('section',{className:'card '+className},children)}
function App(){const[view,setView]=useState('overview'),[state,setState]=useState(null),[games,setGames]=useState([]),[group,setGroup]=useState(null),[toast,setToast]=useState(''),[loading,setLoading]=useState(true);const load=async()=>{setLoading(true);try{const[s,g]=await Promise.all([request('/state'),request('/games')]);setState(s);setGames(g.sessions||[]);if(!group&&s.group)setGroup(s.group)}catch(e){setToast(e.message)}finally{setLoading(false)}};useEffect(()=>{load()},[]);useEffect(()=>{if(toast){const t=setTimeout(()=>setToast(''),2800);return()=>clearTimeout(t)}},[toast]);const nav=(id)=>{setView(id);window.scrollTo(0,0)};const content=view==='overview'?h(Overview,{state,games,loading,nav}):view==='groups'?h(Groups,{state,group,setGroup,setToast}):view==='game'?h(Games,{games}):view==='knowledge'?h(Knowledge,{state,setToast}):h(System,{state,games});return h('div',{className:'app'},h(Sidebar,{view,nav}),h('main',{className:'main'},h(Header,{view,loading,load}),content),h(MobileNav,{view,nav}),toast&&h('div',{className:'toast'},toast));}
const titles={overview:['运行总览','系统健康、告警与实时工作负载'],groups:['常规群管理','管理群功能、消息数据与 AI 行为'],game:['剧本杀模式','管理房间生命周期、阶段与参与者'],knowledge:['画像与知识','维护 Bot 人设和本地知识资产'],system:['系统与日志','版本、模块和运行边界']};
function Sidebar({view,nav}){return h('aside',{className:'sidebar'},h('div',{className:'brand'},'◆ 猎Bot',h('small',null,'CONTROL CENTER')),h('div',{className:'nav-label'},'控制中心'),h(Nav,{id:'overview',view,nav},'◉ 运行总览'),h('div',{className:'nav-label'},'两大工作台'),h(Nav,{id:'groups',view,nav},'▦ 常规群管理'),h(Nav,{id:'game',view,nav,game:true},'◇ 剧本杀模式'),h('div',{className:'nav-label'},'资产与系统'),h(Nav,{id:'knowledge',view,nav},'◎ 画像与知识'),h(Nav,{id:'system',view,nav},'⌁ 系统与日志'))}
function Nav({id,view,nav,game,children}){return h('button',{className:'nav '+(game?'game ':'')+(view===id?'active':''),onClick:()=>nav(id)},children)}
function MobileNav({view,nav}){return h('nav',{className:'mobile-nav'},[['overview','总览'],['groups','群管理'],['game','剧本杀'],['system','更多']].map(([id,t])=>h('button',{key:id,className:(id==='game'?'game ':'')+(view===id?'active':''),onClick:()=>nav(id)},t)))}
function Header({view,loading,load}){const t=titles[view];return h('header',{className:'top'},h('div',{className:'title'},h('h1',null,t[0]),h('p',null,t[1])),h('div',{className:'actions'},h('span',{className:'status'},'● 已连接'),h(Button,{onClick:load,disabled:loading},loading?'刷新中':'刷新')))}
function Metric({label,value,note,color}){return h(Card,{className:'metric'},h('span',{className:'label'},label),h('b',{style:{color}},value),h('small',{className:'muted'},note))}
function Overview({state,games,loading,nav}){
 const groups=state?.groups||[];
 const metrics=h('div',{className:'grid metrics'},
  h(Metric,{label:'Bot 服务',value:loading?'连接中':'在线',note:'NoneBot · OneBot V11',color:'var(--green)'}),
  h(Metric,{label:'已管理群聊',value:groups.length,note:'现有群配置'}),
  h(Metric,{label:'本地 OCR',value:'RapidOCR',note:'图片识别与关键词回怼'}),
  h(Metric,{label:'活跃剧本',value:games.length,note:'运行中或暂停',color:'var(--purple)'})
 );
 const health=h(Card,{className:'hero'},h('h2',null,'模块健康'),h('div',{className:'health'},['QQ / OneBot 连接','消息采集流水线','本地 OCR 引擎','Game Runtime'].map(x=>h('div',{key:x},h('span',{className:'dot'},'● '),x))),h('div',{className:'actions',style:{marginTop:14}},h(Button,{onClick:()=>nav('groups'),kind:'primary'},'进入常规群管理'),h(Button,{onClick:()=>nav('game'),kind:'purple'},'进入剧本杀模式')));
 const attention=h(Card,null,h('h2',null,'需要关注'),h('div',{className:'row warning'},'AI API 余额需通过 QQ 查询确认'),h('div',{className:'row',style:{marginTop:8}},games.length?games.length+' 个剧本房间正在占用 Runtime':'当前没有活跃剧本房间'));
 const workload=h(Card,null,h('h2',null,'当前工作负载'),h('table',{className:'table'},h('tbody',null,h('tr',null,h('td',null,'群功能配置'),h('td',null,groups.length+' 个群')),h('tr',null,h('td',null,'剧本杀会话'),h('td',null,games.length+' 个活跃')),h('tr',null,h('td',null,'知识条目'),h('td',null,(state?.knowledge?.items||[]).length+' 条')))));
 const activity=h(Card,null,h('h2',null,'最近活动'),h('div',{className:'activity'},h('time',null,'刚刚'),h('span',null,'管理中枢完成状态同步')),h('div',{className:'activity'},h('time',null,'持续'),h('span',null,'OCR 本地运行，无外部视觉 API')));
 return h(React.Fragment,null,metrics,h('div',{className:'grid two'},health,attention,workload,activity));
}
function Groups({state,group,setGroup,setToast}){
 const groups=state?.groups||[],[tab,setTab]=useState('features'),[message,setMessage]=useState('');
 const choose=async id=>{try{setGroup(await request('/groups/'+id));setTab('features')}catch(e){setToast(e.message)}};
 const save=async payload=>{try{const next=await request('/groups/'+group.group_id,{method:'PUT',body:JSON.stringify(payload)});setGroup(next);setToast('群配置已保存')}catch(e){setToast(e.message)}};
 const toggle=key=>save({features:{...group.features,[key]:!group.features[key]}});
 const send=async()=>{try{await request('/progress-message',{method:'POST',body:JSON.stringify({group_id:group.group_id,message})});setMessage('');setToast('消息已发送')}catch(e){setToast(e.message)}};
 const tabs=h('div',{className:'tabs'},[['features','功能开关'],['messages','消息与日报'],['members','画像与成员'],['reactions','关键词回怼']].map(([id,t])=>h('button',{key:id,className:'tab '+(tab===id?'active':''),onClick:()=>setTab(id)},t)));
 let pane=null;
 if(group&&tab==='features')pane=h(React.Fragment,null,Object.entries(group.features||{}).map(([k,v])=>h('div',{className:'switch-row',key:k},h('div',null,h('b',null,labels[k]||k),h('div',{className:'muted'},v?'此群已启用':'此群当前关闭')),h('button',{className:'switch '+(v?'on':''),onClick:()=>toggle(k),'aria-label':'切换 '+k},h('i')))));
 if(group&&tab==='messages'){const rows=group.archive?.recent_messages||[];pane=h(React.Fragment,null,h('h3',null,'向群内发送管理消息'),h('div',{className:'actions'},h('input',{className:'search',style:{flex:1},value:message,onChange:e=>setMessage(e.target.value),placeholder:'输入进度通知或主持消息'}),h(Button,{kind:'primary',onClick:send,disabled:!message.trim()},'发送')),h('h3',{style:{marginTop:20}},'最近采集消息 · '+(group.archive?.message_count||0)+' 条'),rows.length?h('table',{className:'table'},h('tbody',null,rows.map(m=>h('tr',{key:m.id},h('td',null,m.display_name||m.user_id||'群友'),h('td',null,m.preview_text||m.plain_text||'—'))))):h('div',{className:'empty'},'暂无消息采集记录'))}
 if(group&&tab==='members'){
  const toggleTarget=async m=>{try{await request('/groups/'+group.group_id+'/companions/'+m.user_id+'/target',{method:'PUT',body:JSON.stringify({enabled:!m.target_enabled,display_name:m.display_name||m.nickname||m.user_id,is_bot:!!m.is_bot,bot_keywords:m.bot_keywords||[]})});await choose(group.group_id);setTab('members');setToast('画像记录状态已更新')}catch(e){setToast(e.message)}};
  const memberRows=(group.members||[]).slice(0,80).map(m=>h('tr',{key:m.user_id},h('td',null,m.display_name||m.nickname||m.card||m.user_id),h('td',null,m.target_enabled?'已记录画像':'未记录'),h('td',null,m.is_bot?'Bot':'群友'),h('td',null,h(Button,{onClick:()=>toggleTarget(m)},m.target_enabled?'停止记录':'开始记录'))));
  pane=h(React.Fragment,null,h('h3',null,'群画像'),h('div',{className:'field'},h('textarea',{value:group.group_profile?.summary||'',onChange:e=>setGroup({...group,group_profile:{...group.group_profile,summary:e.target.value}}),placeholder:'群性质与回复参考'})),h(Button,{onClick:()=>save({group_profile:group.group_profile})},'保存群画像'),h('h3',{style:{marginTop:20}},'群友与画像状态'),h('table',{className:'table'},h('tbody',null,memberRows)));
 }
 if(group&&tab==='reactions'){
  const rules=group.keyword_retort?.rules||[];
  const setRules=next=>setGroup({...group,keyword_retort:{...group.keyword_retort,rules:next}});
  const update=(i,key,value)=>setRules(rules.map((r,n)=>n===i?{...r,[key]:value}:r));
  const updateReply=(i,j,key,value)=>update(i,'replies',(rules[i].replies||[]).map((r,n)=>n===j?{...r,[key]:value}:r));
  const saveRules=()=>save({keyword_retort:{rules:rules.map(rule=>({...rule,replies:(rule.replies||[]).map(r=>({...r,reply_type:r.reply_type||'text',enabled:r.enabled!==false}))}))}});
  const add=()=>setRules([...rules,{id:0,keyword:'',enabled:true,image_match_enabled:true,replies:[{reply_type:'text',content:'',enabled:true}]}]);
  const ruleCards=rules.map((r,i)=>{
   const replies=(r.replies||[]).map((reply,j)=>h('div',{className:'actions',key:j,style:{marginBottom:7}},h('select',{value:reply.reply_type||'text',onChange:e=>updateReply(i,j,'reply_type',e.target.value)},h('option',{value:'text'},'文本'),h('option',{value:'image'},'图片')),h('input',{className:'search',style:{flex:1},value:reply.content||'',onChange:e=>updateReply(i,j,'content',e.target.value),placeholder:reply.reply_type==='image'?'图片 URL 或路径':'回复文字'}),h('label',{className:'muted'},h('input',{type:'checkbox',checked:reply.enabled!==false,onChange:e=>updateReply(i,j,'enabled',e.target.checked)}),' 启用'),h(Button,{kind:'danger',onClick:()=>update(i,'replies',(r.replies||[]).filter((_,n)=>n!==j))},'删除')));
   return h('div',{className:'row',key:r.id||i,style:{marginBottom:8}},h('div',{className:'field'},h('label',null,'关键词'),h('input',{value:r.keyword||'',onChange:e=>update(i,'keyword',e.target.value)})),h('label',{className:'muted'},h('input',{type:'checkbox',checked:r.enabled!==false,onChange:e=>update(i,'enabled',e.target.checked)}),' 启用规则'),h('div',{className:'field'},h('label',null,'回复内容'),replies,h(Button,{onClick:()=>update(i,'replies',[...(r.replies||[]),{reply_type:'text',content:'',enabled:true}])},'＋ 添加回复')),h(Button,{kind:'danger',onClick:()=>setRules(rules.filter((_,n)=>n!==i))},'删除规则'));
  });
  pane=h(React.Fragment,null,h('div',{className:'actions',style:{justifyContent:'space-between'}},h('div',null,h('h3',null,'关键词回怼规则'),h('p',{className:'muted'},'图片文字由本地 RapidOCR 提取。')),h(Button,{onClick:add},'＋ 新建规则')),ruleCards,h(Button,{kind:'primary',onClick:saveRules},'保存全部规则'));
 }
 return h('div',{className:'group-layout'},h(Card,null,h('h2',null,'群列表'),h('div',{className:'list'},groups.map(g=>h('div',{key:g.group_id,className:'list-item '+(group?.group_id===String(g.group_id)?'active':''),onClick:()=>choose(g.group_id)},h('b',null,g.group_name||('群 '+g.group_id)),h('div',{className:'muted'},g.group_id))))),h(Card,null,group?h(React.Fragment,null,h('h2',null,'群 '+group.group_id),tabs,pane):h('div',{className:'empty'},'选择一个群开始管理')));
}
function Games({games}){
 const toolbar=h('div',{className:'actions',style:{justifyContent:'space-between',marginBottom:14}},h('div',{className:'tabs'},h('button',{className:'tab game active'},'Runtime 状态'),h('button',{className:'tab game'},'规则与资产')),h('span',{className:'muted'},'控制操作请通过正式 DM / QQ 控制链执行'));
 const rows=games.map(g=>h('tr',{key:g.game_id},
  h('td',null,h('b',null,'群 '+g.group_id),h('div',{className:'muted'},g.game_id.slice(0,18)+'…')),
  h('td',null,g.phase),
  h('td',null,h('span',{className:'pill '+(g.status==='PAUSED'?'paused':'')},g.status)),
  h('td',null,h('span',{className:'muted'},'state v'+g.state_version))
 ));
 const body=games.length?h(Card,null,h('table',{className:'table'},h('thead',null,h('tr',null,h('th',null,'群 / 游戏'),h('th',null,'阶段'),h('th',null,'状态'),h('th',null,'版本'))),h('tbody',null,rows))):h('div',{className:'empty'},h('h3',null,'暂无未结束剧本'),h('p',null,'正式控制链创建或启动游戏后，这里会自动显示运行状态。'));
 return h(React.Fragment,null,toolbar,body);
}
function Knowledge({state,setToast}){
 const[p,setP]=useState(state?.persona||''),[items,setItems]=useState(flattenKnowledge(state?.knowledge?.tree||{})),[edit,setEdit]=useState(null);
 const savePersona=async()=>{try{await request('/persona',{method:'PUT',body:JSON.stringify({persona:p})});setToast('Bot 人设已保存')}catch(e){setToast(e.message)}};
 const open=async item=>{if(!item){setEdit({title:'',category:'自定义/通用',keywords:'',content:'',enabled:true});return}try{const d=await request('/knowledge/'+item.id);const x=d.item||d;setEdit({...x,keywords:Array.isArray(x.keywords)?x.keywords.join('，'):x.keywords||''})}catch(e){setToast(e.message)}};
 const saveItem=async()=>{const payload={...edit,keywords:String(edit.keywords||'').split(/[，,]/).map(x=>x.trim()).filter(Boolean),enabled:edit.enabled!==false};try{const result=await request('/knowledge'+(edit.id?'/'+edit.id:''),{method:edit.id?'PUT':'POST',body:JSON.stringify(payload)});setToast('知识条目已保存');const k=await request('/knowledge');setItems(flattenKnowledge(k.tree||{}));setEdit(result.item||result)}catch(e){setToast(e.message)}};
 const remove=async()=>{if(!edit?.id||!confirm('确定删除该知识条目？'))return;try{await request('/knowledge/'+edit.id,{method:'DELETE'});const k=await request('/knowledge');setItems(flattenKnowledge(k.tree||{}));setEdit(null);setToast('知识条目已删除')}catch(e){setToast(e.message)}};
 const left=h(Card,null,h('h2',null,'Bot 人设'),h('div',{className:'field'},h('textarea',{style:{minHeight:180},value:p,onChange:e=>setP(e.target.value)})),h(Button,{kind:'primary',onClick:savePersona},'保存人设'),h('div',{className:'actions',style:{justifyContent:'space-between',marginTop:22}},h('h2',null,'知识条目'),h(Button,{onClick:()=>open(null)},'＋ 新建')),h('div',{className:'list'},items.slice(0,120).map(x=>h('div',{className:'list-item',key:x.id,onClick:()=>open(x)},h('b',null,x.title),h('div',{className:'muted'},x.category||'未分类')))));
 const editor=edit?h(React.Fragment,null,h('h2',null,edit.id?'编辑知识':'新建知识'),['title','category','keywords'].map(key=>h('div',{className:'field',key},h('label',null,{title:'标题',category:'分类',keywords:'关键词'}[key]),h('input',{value:edit[key]||'',onChange:e=>setEdit({...edit,[key]:e.target.value})}))),h('div',{className:'field'},h('label',null,'正文'),h('textarea',{style:{minHeight:260},value:edit.content||'',onChange:e=>setEdit({...edit,content:e.target.value})})),h('div',{className:'actions'},h(Button,{kind:'primary',onClick:saveItem},'保存条目'),edit.id&&h(Button,{kind:'danger',onClick:remove},'删除'))):h('div',{className:'empty'},'选择或新建知识条目');
 return h('div',{className:'grid two'},left,h(Card,null,editor));
}
function flattenKnowledge(tree){const out=[];Object.values(tree||{}).forEach(group=>Object.values(group||{}).forEach(list=>(Array.isArray(list)?list:[]).forEach(x=>out.push(x))));return out}
function System({state,games}){return h('div',{className:'grid two'},h(Card,null,h('h2',null,'运行信息'),h('table',{className:'table'},h('tbody',null,h('tr',null,h('td',null,'应用版本'),h('td',null,state?.version||'—')),h('tr',null,h('td',null,'管理路由'),h('td',null,state?.route_prefix||'—')),h('tr',null,h('td',null,'Game Runtime'),h('td',null,games.length+' 个活跃会话')),h('tr',null,h('td',null,'管理员模型'),h('td',null,'单一超级管理员'))))),h(Card,null,h('h2',null,'安全边界'),h('div',{className:'row'},'管理令牌通过 HttpOnly 会话 Cookie 保护'),h('div',{className:'row',style:{marginTop:8}},'危险游戏操作要求二次确认'),h('div',{className:'row',style:{marginTop:8}},'页面不展示 API Key 与真实密钥')))}
ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();

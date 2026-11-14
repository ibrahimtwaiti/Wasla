"""Validated WhatsApp template components and bounded, origin-safe pagination."""
import re
from urllib.parse import quote
from fastapi import HTTPException
from .db import one

def pages(account,edge):
    from .service import provider_request
    found=[];cursor='';seen=set()
    for _ in range(50):
        r=provider_request(account,'GET','/'+edge+'?limit=100'+('&after='+quote(cursor,safe='') if cursor else ''))
        if r.status_code!=200:raise HTTPException(502,'provider_pagination_failed')
        page=r.json()
        if not isinstance(page.get('data'),list):raise HTTPException(502,'invalid_provider_page')
        found.extend(page['data']);paging=page.get('paging',{})
        if not paging.get('next'):return found
        cursor=paging.get('cursors',{}).get('after','')
        if not isinstance(cursor,str) or not cursor or len(cursor)>4000 or cursor in seen:raise HTTPException(502,'invalid_provider_cursor')
        seen.add(cursor)
    raise HTTPException(409,'provider_pagination_limit_retry_with_smaller_catalog')

def text_parameters(text,values):
    expected=sorted(set(int(n) for n in re.findall(r'\{\{(\d+)\}\}',text)))
    if not isinstance(values,list) or any(not isinstance(v,str) or not 1<=len(v)<=1000 for v in values):raise HTTPException(422,'invalid_template_parameters')
    if expected!=list(range(1,len(values)+1)) or re.search(r'\{\{[^\d}]',text):raise HTTPException(422,'template_parameters_mismatch')
    return [{'type':'text','text':v} for v in values]

def components(db,template,payload,ticket,resolve_media=False):
    result=[];header=payload.get('header_parameters',[]);buttons=payload.get('button_parameters',{})
    if not isinstance(buttons,dict):raise HTTPException(422,'invalid_button_parameters')
    used=set();header_used=False
    for c in template:
        kind=c.get('type')
        if kind=='FOOTER':continue
        if kind=='BODY':
            ps=text_parameters(c.get('text',''),payload.get('parameters',[]))
            if ps:result.append({'type':'body','parameters':ps})
        elif kind=='HEADER':
            header_used=True;fmt=c.get('format','TEXT')
            if fmt=='TEXT':ps=text_parameters(c.get('text',''),header)
            elif fmt in {'IMAGE','DOCUMENT'}:
                m=one(db,"SELECT * FROM service_media WHERE id=? AND tenant_id=? AND ticket_id=? AND scan_state='clean'",(payload.get('header_attachment_id'),ticket['tenant_id'],ticket['id']))
                if not m or (fmt=='IMAGE')!=m['mime_type'].startswith('image/'):raise HTTPException(422,'template_header_clean_attachment_required')
                from .service_media import media_url
                ps=[{'type':fmt.lower(),fmt.lower():{'link':media_url(m) if resolve_media else 'validated-at-dispatch'}}]
                if header:raise HTTPException(422,'unexpected_header_parameters')
            else:raise HTTPException(422,'unsupported_template_header_format')
            if ps:result.append({'type':'header','parameters':ps})
        elif kind=='BUTTONS':
            for i,b in enumerate(c.get('buttons',[])):
                key=str(i);value=buttons.get(key);typ=b.get('type')
                if typ=='URL' and '{{' in b.get('url',''):
                    ps=text_parameters(b['url'],[value] if value is not None else [])
                    result.append({'type':'button','sub_type':'url','index':key,'parameters':ps});used.add(key)
                elif typ=='QUICK_REPLY' and value is not None:
                    if not isinstance(value,str) or not 1<=len(value)<=128:raise HTTPException(422,'invalid_quick_reply_payload')
                    result.append({'type':'button','sub_type':'quick_reply','index':key,'parameters':[{'type':'payload','payload':value}]});used.add(key)
                elif typ not in {'URL','PHONE_NUMBER','QUICK_REPLY'}:raise HTTPException(422,'unsupported_template_button_type')
        else:raise HTTPException(422,'unsupported_template_component')
    if set(buttons)!=used or (header and not header_used):raise HTTPException(422,'unexpected_template_parameters')
    return result

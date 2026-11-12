"""Validated, provider-managed visual identity; no arbitrary CSS/HTML execution."""
import base64
import binascii
import json
import re
import xml.etree.ElementTree as ET
from fastapi import HTTPException

DEFAULTS = dict(name='وصلة', name_en='Wasla', primary='#834DFB', secondary='#F0E100', ink='#1B102B', background='#F5F3FF', surface='#FFFFFF', muted='#70677E', radius=24, logo_width=166, logo_padding=4, logo_position='start', font_ar='Tajawal', font_en='Manrope', icon_style='outline', shadow='soft', logo_light='', logo_dark='', hero_image='', social_instagram='', social_linkedin='', social_x='', sections=['services','about','benefits','process','faq','contact'])
COLORS={'primary','secondary','ink','background','surface','muted'}
ASSETS={'logo_light','logo_dark','hero_image'}
CHOICES={'logo_position':{'start','center'},'font_ar':{'Tajawal','Tahoma','Arial'},'font_en':{'Manrope','Arial','Tahoma'},'icon_style':{'outline','soft'},'shadow':{'soft','none'}}

def asset(value):
    if value=='': return value
    if not isinstance(value,str) or len(value)>700000: raise ValueError('ملف الصورة أكبر من المسموح')
    if value.startswith('/static/brand/') and re.fullmatch(r'/static/brand/[a-zA-Z0-9_.-]+',value): return value
    match=re.fullmatch(r'data:image/(png|svg\+xml);base64,([A-Za-z0-9+/=]+)',value)
    if not match: raise ValueError('استخدم شعار PNG أو SVG')
    try: raw=base64.b64decode(match[2],validate=True)
    except binascii.Error: raise ValueError('ملف غير صالح')
    if len(raw)>500000: raise ValueError('الحد الأقصى 500 كيلوبايت للصورة')
    if match[1]=='png':
        if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw)<24: raise ValueError('PNG غير صالح')
        width=int.from_bytes(raw[16:20],'big');height=int.from_bytes(raw[20:24],'big')
        if not 0<width<=6000 or not 0<height<=6000: raise ValueError('أبعاد الصورة غير صالحة')
    else:
        if b'<!' in raw: raise ValueError('SVG غير آمن')
        try: root=ET.fromstring(raw)
        except ET.ParseError: raise ValueError('SVG غير صالح')
        allowed={'svg','g','path','rect','circle','ellipse','line','polyline','polygon','defs','linearGradient','radialGradient','stop','title','desc','clipPath'}
        if root.tag.split('}')[-1]!='svg': raise ValueError('SVG غير صالح')
        for el in root.iter():
            if el.tag.split('}')[-1] not in allowed: raise ValueError('SVG يحتوي عناصر غير مدعومة')
            for k,v in el.attrib.items():
                if k.lower().startswith('on') or k.split('}')[-1] in {'href','style'} or '<' in v or ('url(' in v and not re.fullmatch(r'url\(#[\w-]+\)',v)): raise ValueError('SVG غير آمن')
    return value

def validate(data):
    if not isinstance(data,dict) or set(data)-set(DEFAULTS): raise HTTPException(422,'إعدادات هوية غير معروفة')
    result=DEFAULTS.copy()
    try:
        for k,v in data.items():
            if k in COLORS:
                if not isinstance(v,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',v): raise ValueError('لون غير صالح')
            elif k in ASSETS: v=asset(v)
            elif k in CHOICES:
                if v not in CHOICES[k]: raise ValueError('خيار غير صالح')
            elif k in {'radius','logo_width','logo_padding'}:
                lo,hi={'radius':(0,40),'logo_width':(100,230),'logo_padding':(0,20)}[k]
                if type(v) is not int or not lo<=v<=hi: raise ValueError('قيمة خارج النطاق')
            elif k=='sections':
                if not isinstance(v,list) or len(v)!=len(set(v)) or any(s not in DEFAULTS['sections'] for s in v): raise ValueError('ترتيب أقسام غير صالح')
            elif k.startswith('social_'):
                if not isinstance(v,str) or (v and not re.fullmatch(r'https://[a-zA-Z0-9.-]+(?:/[^\s<>"\\]*)?',v)) or len(v)>500: raise ValueError('رابط HTTPS غير صالح')
            elif not isinstance(v,str) or not 1<=len(v)<=80: raise ValueError('اسم العلامة غير صالح')
            result[k]=v
    except (ValueError,TypeError) as e: raise HTTPException(422,str(e))
    return result

def read(db):
    row=db.execute("SELECT value FROM content WHERE key='brand'").fetchone()
    return {**DEFAULTS,**json.loads(row['value'])} if row else DEFAULTS.copy()

def css(b):
    tokens={'brand':b['primary'],'brand-hover':b['ink'],'accent':b['secondary'],'ink':b['ink'],'paper':b['background'],'surface':b['surface'],'muted':b['muted'],'radius':str(b['radius'])+'px','logo-width':str(b['logo_width'])+'px','logo-padding':str(b['logo_padding'])+'px','font':b['font_ar']+', '+b['font_en']+', sans-serif','font-en':b['font_en']+', sans-serif','shadow':'0 16px 48px #1b102b0b' if b['shadow']=='soft' else 'none'}
    return ':root{'+''.join('--'+k+':'+v+';' for k,v in tokens.items())+'}'

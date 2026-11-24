"""Synthetic-only, per-user patient persistence. No scans or client-owned keys."""
import os, json, re, hashlib
from datetime import datetime, timezone, date
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

class Invalid(ValueError): pass
class Missing(ValueError): pass
class Conflict(ValueError): pass

def now():
    return datetime.now(timezone.utc).isoformat()

def clean_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,80}',value):
        raise Invalid('Invalid identifier')
    return value

def safe_json(value):
    return json.loads(json.dumps(value, default=lambda x: float(x) if isinstance(x,Decimal) else str(x)))

def bounded(value, limit=30000):
    if not isinstance(value,dict) or len(json.dumps(value,default=str))>limit:
        raise Invalid('Invalid record')
    try: return json.loads(json.dumps(value,allow_nan=False),parse_float=Decimal)
    except (TypeError,ValueError): raise Invalid('Invalid JSON record')

def metrics(value):
    if not isinstance(value,dict): raise Invalid('Invalid metrics')
    out={}
    for k,lo,hi in [('pps',0,100),('weight_lb',0.01,1500),('mac_cm',0.01,150),('adl_dependence',0,6)]:
        if k not in value or value[k] in ('',None): continue
        v=value[k]
        if isinstance(v,bool) or not isinstance(v,(int,float,Decimal)) or not Decimal(str(v)).is_finite() or not lo<=v<=hi: raise Invalid('Invalid '+k)
        if k=='pps' and v%10: raise Invalid('PPS must be in increments of ten')
        if k=='adl_dependence' and v%1: raise Invalid('ADL count must be an integer')
        out[k]=Decimal(str(v))
    for k,allowed in [('fast',['1','2','3','4','5','6A','6B','6C','6D','6E','7A','7B','7C','7D','7E','7F']),('nyha',['I','II','III','IV'])]:
        if value.get(k) in ('',None): continue
        if value[k] not in allowed: raise Invalid('Invalid '+k)
        out[k]=value[k]
    return out

def history_order(row):
    return (row['assessment_date'],row.get('finalized_at',row['created_at']),row['visit_id'])

def history_fingerprint(rows):
    return hashlib.sha256(json.dumps(sorted((x['visit_id'],x['version']) for x in rows)).encode()).hexdigest()

def compare(current, previous):
    result={}
    for k,v in current.items():
        if k not in previous: continue
        old=previous[k]
        result[k]={'previous':old,'current':v,'changed':v!=old}
        if k in ('pps','weight_lb','mac_cm','adl_dependence'):
            result[k]['delta']=v-old
            if k=='weight_lb' and old>0: result[k]['percent']=round((v-old)/old*100,2)
    return result

class Store:
    def __init__(self, sub, table=None):
        self.sub=clean_id(sub)
        self.pk='USER#'+self.sub
        self.table=table if table is not None else boto3.resource('dynamodb').Table(os.environ['CLINICAL_TABLE_NAME'])
    def key(self,sk): return {'PK':self.pk,'SK':sk}
    def get(self,sk):
        row=self.table.get_item(Key=self.key(sk),ConsistentRead=True).get('Item')
        if not row: raise Missing('Record not found')
        return row
    def put(self,sk,data,version=None):
        row={**data,**self.key(sk),'updated_at':now(),'updated_by':self.sub}
        args={'Item':row}
        if version is None: args['ConditionExpression']='attribute_not_exists(PK)'
        else:
            args.update(ConditionExpression='#v = :v AND #s = :draft',ExpressionAttributeNames={'#v':'version','#s':'status'},ExpressionAttributeValues={':v':version,':draft':'draft'})
        try: self.table.put_item(**args)
        except ClientError as e:
            if e.response['Error']['Code']=='ConditionalCheckFailedException': raise Conflict('Record already exists or version changed')
            raise
        return row
    def query(self,prefix):
        args={'KeyConditionExpression':Key('PK').eq(self.pk)&Key('SK').begins_with(prefix),'ConsistentRead':True,'Limit':100}
        while True:
            page=self.table.query(**args)
            yield from page.get('Items',[])
            if not page.get('LastEvaluatedKey'): break
            args['ExclusiveStartKey']=page['LastEvaluatedKey']
    def dispatch(self,body,review):
        action=body.get('action')
        if action=='command_center':
            profiles=list(self.query('PROFILE#'))
            rows=[]
            for p in profiles:
                visits=[v for v in self.query('VISIT#'+p['patient_id']+'#') if v['status']=='final']
                visits.sort(key=history_order)
                rows.append({'patient_id':p['patient_id'],'name':p['profile']['name'],'diagnosis':p['profile']['diagnosis'],'status':p['status'],'last_visit':visits[-1]['assessment_date'] if visits else None,'benefit_period':visits[-1]['benefit_period'] if visits else None,'visit_count':len(visits),'last_idg':next((v['assessment_date'] for v in reversed(visits) if v['visit_type']=='IDG'),None),'last_f2f':next((v['assessment_date'] for v in reversed(visits) if v['visit_type']=='Face-to-face'),None),'trajectory':[{'date':v['assessment_date'],'metrics':v['metrics']} for v in visits if v['metrics']][-12:]})
            return {'patients':rows,'regulatory_metrics_enabled':False}
        if action=='list_patients':
            return {'patients':list(self.query('PROFILE#'))}
        pid=clean_id(body.get('patient_id'))
        profile_key='PROFILE#'+pid
        if action=='create_patient':
            p=bounded(body.get('profile'))
            if not all(isinstance(p.get(k),str) and p[k].strip() for k in ('name','diagnosis','sex')): raise Invalid('Missing profile fields')
            if type(p.get('age')) is not int or not 0<=p['age']<=120: raise Invalid('Invalid age')
            if not isinstance(p.get('comorbidities'),list) or not all(isinstance(x,str) and x.strip() for x in p['comorbidities']): raise Invalid('Invalid comorbidities')
            p={k:p[k] for k in ('name','diagnosis','sex','age','comorbidities')}
            return {'patient':self.put(profile_key,{'patient_id':pid,'profile':p,'created_at':now(),'created_by':self.sub,'status':'active'})}
        profile=self.get(profile_key)
        if action=='set_patient_status':
            status=body.get('status')
            if status not in ('active','inactive','discharged','deceased'): raise Invalid('Invalid patient status')
            if profile['status'] in ('discharged','deceased') and status!=profile['status']: raise Conflict('Closed patient status requires a future audited reopening workflow')
            if status in ('discharged','deceased'):
                reason=body.get('reason')
                if not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise Invalid('Closure reason required')
                if status=='discharged' and not any(v['status']=='final' and v['visit_type']=='Discharge' for v in self.query('VISIT#'+pid+'#')): raise Conflict('Finalize discharge documentation first')
                self.put('STATUS#'+pid+'#'+clean_id(body.get('event_id')),{'patient_id':pid,'from_status':profile['status'],'to_status':status,'reason':reason,'created_at':now(),'created_by':self.sub})
            row=self.table.update_item(Key=self.key(profile_key),UpdateExpression='SET #s=:s, updated_at=:at, updated_by=:by',
                ConditionExpression='attribute_exists(PK)',ExpressionAttributeNames={'#s':'status'},
                ExpressionAttributeValues={':s':status,':at':now(),':by':self.sub},ReturnValues='ALL_NEW')
            return {'patient':row['Attributes']}
        prefix='VISIT#'+pid+'#'
        if action=='add_addendum':
            target=clean_id(body.get('visit_id'))
            visit=self.get(prefix+target)
            if visit['status']!='final': raise Conflict('Only finalized records accept addenda')
            note=body.get('note')
            if not isinstance(note,str) or not note.strip() or len(note)>4000: raise Invalid('Addendum text required')
            aid=clean_id(body.get('addendum_id'))
            return {'addendum':self.put('ADDENDUM#'+pid+'#'+target+'#'+aid,{'patient_id':pid,'visit_id':target,'note':note,'created_at':now(),'created_by':self.sub})}
        if action=='get_chart':
            rows=list(self.query(prefix))
            final=sorted((x for x in rows if x['status']=='final'),key=history_order)
            baseline=next((x for x in final if x['visit_type']=='Admission'),None)
            return {'patient':profile,'visits':rows,'baseline':baseline,'addenda':list(self.query('ADDENDUM#'+pid+'#')),'benefit_periods':sorted(set(x['benefit_period'] for x in final)),'status_history':list(self.query('STATUS#'+pid+'#'))}
        if profile['status'] in ('discharged','deceased'): raise Conflict('Patient is closed; only chart review and addenda are available')
        if action not in ('save_draft','review_visit','finalize_visit'): raise Invalid('Unknown action')
        vid=clean_id(body.get('visit_id'))
        sk=prefix+vid
        version=body.get('version',0)
        if type(version) is not int or version<0: raise Invalid('Invalid version')
        previous=self.get(sk) if version else None
        if previous and (previous['status']!='draft' or previous['version']!=version): raise Conflict('Draft changed or finalized')
        data=bounded(body.get('assessment'))
        visit_type=data.get('visit_type')
        if visit_type not in ('Admission','Routine nursing visit','PRN visit','Recertification','Discharge','IDG','Face-to-face','CTI evidence summary'): raise Invalid('Invalid visit type')
        try: day=date.fromisoformat(data['assessment_date']).isoformat()
        except (KeyError,ValueError,TypeError): raise Invalid('Invalid assessment date')
        if day>datetime.now(timezone.utc).date().isoformat(): raise Invalid('Future assessment date')
        findings=data.get('assessment','')
        if not isinstance(findings,str) or len(findings)>10000: raise Invalid('Invalid findings')
        bp=data.get('benefit_period')
        if type(bp) is not int or not 1<=bp<=100: raise Invalid('Invalid benefit period')
        current=metrics(data.get('metrics',{}))
        if 'weight_lb' in current:
            try: weight_date=date.fromisoformat(data.get('form',{}).get('clinical',{}).get('currentDate','')).isoformat()
            except (ValueError,TypeError,AttributeError): raise Invalid('Current weight requires a measurement date')
            if weight_date!=day: raise Invalid('Current structured weight must be measured on assessment date')
        row={'patient_id':pid,'visit_id':vid,'version':version+1,'status':'draft','assessment_date':day,'visit_type':visit_type,'benefit_period':bp,'assessment':findings,'metrics':current,'form':bounded(data.get('form',{})),'created_at':previous['created_at'] if previous else now(),'created_by':self.sub}
        if action=='finalize_visit':
            history=list(x for x in self.query(prefix) if x['status']=='final')
            if previous and previous.get('history_fingerprint')!=history_fingerprint(history):
                raise Conflict('Chart history changed; review again')
            if not previous or previous.get('review',{}).get('status')!='draft_ready':
                raise Conflict('Review this draft before finalizing')
            for k in ('assessment_date','visit_type','benefit_period','assessment','metrics','form'):
                if row[k]!=previous[k]: raise Conflict('Findings changed; review again')
            row.update(status='final',review=previous['review'],finalized_at=now(),finalized_by=self.sub,
                       comparison=previous['comparison'],baseline_comparison=previous['baseline_comparison'],
                       prior_visit_id=previous['prior_visit_id'],baseline_visit_id=previous['baseline_visit_id'])
        if action=='review_visit':
            if not findings.strip(): raise Invalid('Assessment findings required')
            all_history=[x for x in self.query(prefix) if x['status']=='final']
            row['history_fingerprint']=history_fingerprint(all_history)
            history=sorted((x for x in all_history if x['assessment_date']<=day),key=history_order)
            clinical_history=[x for x in history if x['visit_type'] not in ('IDG','CTI evidence summary')]
            candidates=([x for x in history if x['visit_type']=='IDG'] if visit_type=='IDG' else [x for x in clinical_history if x['benefit_period']<bp] if visit_type=='Recertification' else clinical_history)
            prior=candidates[-1] if candidates else None
            baseline=next((x for x in history if x['visit_type']=='Admission'),None)
            row['comparison']=compare(current,prior['metrics']) if prior else {}
            row['baseline_comparison']=compare(current,baseline['metrics']) if baseline else {}
            row['prior_visit_id']=prior['visit_id'] if prior else None
            row['baseline_visit_id']=baseline['visit_id'] if baseline else None
            patient={**profile['profile'],'patient_id':pid,'visit_type':visit_type,'benefit_period':bp,'assessment':findings}
            patient.pop('name',None)
            patient['current_structured_findings']=safe_json(current)
            if 'pps' in current: patient['current_pps']=int(current['pps'])
            if prior and 'pps' in prior['metrics']: patient['previous_pps']=int(prior['metrics']['pps'])
            if row['comparison']: patient['verified_prior_comparison']={'assessment_date':prior['assessment_date'],'metrics':safe_json(row['comparison'])}
            if row['baseline_comparison']: patient['verified_admission_comparison']={'assessment_date':baseline['assessment_date'],'metrics':safe_json(row['baseline_comparison'])}
            if visit_type in ('IDG','CTI evidence summary','Face-to-face','Recertification'):
                patient['dated_prior_findings']=[{'date':x['assessment_date'],'type':x['visit_type'],'benefit_period':x['benefit_period'],'metrics':safe_json(x['metrics']),'assessment':x['assessment'][:1500]} for x in history[-8:]]
            output=review(safe_json(patient))
            if output.get('status')!='draft_ready': return {'review':output}
            row['review']=bounded(output,60000)
        return {'visit':self.put(sk,row,version if version else None)}

"""Offline synthetic tests; no boto3 installation or cloud credentials required."""
import sys, types, unittest, json, copy
from unittest.mock import patch, Mock
from decimal import Decimal

class ClientError(Exception):
    def __init__(self,response,operation='PutItem'):
        self.response=response
        super().__init__(operation)

class Condition:
    def __init__(self,parts): self.parts=parts
    def __and__(self,other): return Condition(self.parts+other.parts)
class Key:
    def __init__(self,name): self.name=name
    def eq(self,value): return Condition([(self.name,'eq',value)])
    def begins_with(self,value): return Condition([(self.name,'prefix',value)])
boto=types.ModuleType('boto3')
boto.resource=Mock(side_effect=AssertionError('Unexpected AWS resource call'))
boto.client=Mock(side_effect=AssertionError('Unexpected AWS client call'))
dynamo=types.ModuleType('boto3.dynamodb')
conditions=types.ModuleType('boto3.dynamodb.conditions');conditions.Key=Key
botocore=types.ModuleType('botocore')
exceptions=types.ModuleType('botocore.exceptions');exceptions.ClientError=ClientError
sys.modules.update({'boto3':boto,'boto3.dynamodb':dynamo,'boto3.dynamodb.conditions':conditions,'botocore':botocore,'botocore.exceptions':exceptions})

import patient_store as ps
import lambda_function as lf

class FakeTable:
    def __init__(self): self.rows={};self.pages=0
    def get_item(self,Key,**kw): return {'Item':copy.deepcopy(self.rows.get((Key['PK'],Key['SK'])))}
    def put_item(self,Item,ConditionExpression,**kw):
        key=(Item['PK'],Item['SK']);old=self.rows.get(key)
        if ConditionExpression=='attribute_not_exists(PK)': ok=old is None
        else: ok=old is not None and old['status']=='draft' and old['version']==kw['ExpressionAttributeValues'][':v']
        if not ok: raise ClientError({'Error':{'Code':'ConditionalCheckFailedException'}})
        self.rows[key]=copy.deepcopy(Item)
    def query(self,KeyConditionExpression,ExclusiveStartKey=None,**kw):
        self.pages+=1
        pk=KeyConditionExpression.parts[0][2];prefix=KeyConditionExpression.parts[1][2]
        keys=sorted(k for k in self.rows if k[0]==pk and k[1].startswith(prefix))
        if ExclusiveStartKey: keys=[k for k in keys if k>(ExclusiveStartKey['PK'],ExclusiveStartKey['SK'])]
        page={'Items':[copy.deepcopy(self.rows[k]) for k in keys[:1]]}
        if len(keys)>1: page['LastEvaluatedKey']={'PK':keys[0][0],'SK':keys[0][1]}
        return page
    def update_item(self,Key,ExpressionAttributeValues,**kw):
        row=self.rows[(Key['PK'],Key['SK'])]
        row.update(status=ExpressionAttributeValues[':s'],updated_at=ExpressionAttributeValues[':at'],updated_by=ExpressionAttributeValues[':by'])
        return {'Attributes':copy.deepcopy(row)}

def output(patient):
    return {'status':'draft_ready','narrative':'Synthetic narrative','interventions':[],'documentation_flags':[],'recommended_assessments':[],'follow_up_questions':[]}

def event(body,sub='test-user'):
    return {'requestContext':{'apiId':'4g8wxib8td','authorizer':{'jwt':{'claims':{'sub':sub,'token_use':'access','iss':'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_J3HRGAyFZ','client_id':'328ea78t9fbo3cb123pm6vhriu','cognito:groups':['Administrators']}}}},'routeKey':'POST /review','body':json.dumps({'synthetic_data':True,**body})}

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.table=FakeTable();self.store=ps.Store('test-user',self.table)
        self.profile={'name':'Fictional patient','age':84,'sex':'Female','diagnosis':'Alzheimer disease','comorbidities':[]}
        self.dispatch('create_patient',profile=self.profile)
    def dispatch(self,action,**kw):
        return self.store.dispatch({'action':action,'patient_id':'fictional-1',**kw},output)
    def assessment(self,day='2026-01-01',visit_type='Admission',weight=100):
        return {'assessment_date':day,'visit_type':visit_type,'benefit_period':1,'assessment':'FAST: 7C','metrics':{'pps':40,'fast':'7C','weight_lb':weight,'mac_cm':25,'adl_dependence':5},'form':{'clinical':{'currentDate':day}}}
    def finalize(self,vid,data):
        reviewed=self.dispatch('review_visit',visit_id=vid,assessment=data)['visit']
        return self.dispatch('finalize_visit',visit_id=vid,version=reviewed['version'],assessment=data)['visit']
    def test_persistence_and_isolation(self):
        new=ps.Store('test-user',self.table)
        self.assertEqual(new.dispatch({'action':'list_patients'},output)['patients'][0]['profile'],self.profile)
        other=ps.Store('other-user',self.table)
        self.assertEqual(other.dispatch({'action':'list_patients'},output)['patients'],[])
        with self.assertRaises(ps.Missing): other.dispatch({'action':'get_chart','patient_id':'fictional-1'},output)
    def test_duplicate_profile(self):
        with self.assertRaises(ps.Conflict): self.dispatch('create_patient',profile=self.profile)
    def test_draft_stale_and_final_immutable(self):
        a=self.assessment()
        draft=self.dispatch('save_draft',visit_id='v1',assessment=a)['visit']
        self.assertEqual(draft['version'],1)
        with self.assertRaises(ps.Conflict): self.dispatch('save_draft',visit_id='v1',assessment=a)
        reviewed=self.dispatch('review_visit',visit_id='v1',version=1,assessment=a)['visit']
        with self.assertRaises(ps.Conflict): self.dispatch('finalize_visit',visit_id='v1',version=2,assessment={**a,'assessment':'changed'})
        final=self.store.dispatch({'action':'finalize_visit','patient_id':'fictional-1','visit_id':'v1','version':2,'assessment':a},Mock(side_effect=AssertionError('Must not regenerate')))['visit']
        self.assertEqual(final['review'],reviewed['review'])
        self.assertEqual(final['status'],'final')
        with self.assertRaises(ps.Conflict): self.dispatch('save_draft',visit_id='v1',version=3,assessment=a)
    def test_longitudinal_and_pagination(self):
        self.finalize('v1',self.assessment())
        row=self.finalize('v2',self.assessment('2026-02-01','Routine nursing visit',90))
        self.assertEqual(row['comparison']['weight_lb']['percent'],Decimal('-10'))
        self.assertEqual(row['baseline_visit_id'],'v1')
        self.assertEqual(row['comparison']['pps']['changed'],False)
        chart=self.dispatch('get_chart')
        self.assertEqual(len(chart['visits']),2)
        self.assertEqual(chart['baseline']['visit_id'],'v1')
        self.assertGreater(self.table.pages,2)
    def test_missing_values_not_carried(self):
        self.finalize('v1',self.assessment())
        a=self.assessment('2026-02-01','Routine nursing visit');a['metrics']={}
        row=self.finalize('v2',a)
        self.assertEqual(row['comparison'],{})
        self.assertEqual(row['metrics'],{})
    def test_invalid_measurements(self):
        for key,value in [('pps',45),('weight_lb',0),('fast','7Z'),('adl_dependence',7)]:
            a=self.assessment();a['metrics'][key]=value
            with self.assertRaises(ps.Invalid): self.dispatch('save_draft',visit_id='bad',assessment=a)
    def test_unreviewed_finalize(self):
        a=self.assessment()
        self.dispatch('save_draft',visit_id='v1',assessment=a)
        with self.assertRaises(ps.Conflict): self.dispatch('finalize_visit',visit_id='v1',version=1,assessment=a)
    def test_status(self):
        self.assertEqual(self.dispatch('set_patient_status',status='inactive')['patient']['status'],'inactive')
    def test_auth_and_synthetic_gate(self):
        self.assertEqual(lf.lambda_handler({'synthetic_data':True},None)['statusCode'],401)
        e=event({'action':'list_patients'});e['requestContext']['authorizer']['jwt']['claims'].pop('sub')
        self.assertEqual(lf.lambda_handler(e,None)['statusCode'],401)
        e=event({'action':'list_patients'});e['requestContext']['authorizer']['jwt']['claims']['cognito:groups']=[]
        self.assertEqual(lf.lambda_handler(e,None)['statusCode'],403)
        self.assertEqual(lf.lambda_handler(event({'synthetic_data':False}),None)['statusCode'],400)
        with patch.object(lf,'Store',return_value=self.store):
            self.assertEqual(lf.lambda_handler(event({'action':'list_patients'}),None)['statusCode'],200)
    def test_legacy_review_and_clinical_validation(self):
        p={'diagnosis':'Alzheimer disease','visit_type':'Admission','assessment':'No stage'}
        self.assertTrue(lf.required(p))
        p['assessment']='FAST: 7C';self.assertFalse(lf.required(p))
        p['visit_type']='Routine nursing visit';p['assessment']='Stable';self.assertFalse(lf.required(p))
        with patch.object(lf,'call_ai',side_effect=output):
            self.assertEqual(lf.lambda_handler(event({'patient':p}),None)['statusCode'],200)
    def test_no_client_identity(self):
        a=self.assessment()
        row=self.dispatch('save_draft',visit_id='v1',assessment={**a,'created_by':'attacker'},created_by='attacker')['visit']
        self.assertEqual(row['created_by'],'test-user')

if __name__=='__main__': unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from dataclasses import replace
from jobbot.filtering import Rules,assess,refine,render,REJECTED
from jobbot.state import Store
from jobbot.config import load_config
from jobbot.localmodel import classify
from jobbot.applications import prepare
from jobbot.telegram import BotAPI
from test_bot import post,NOW
from test_applications import fixture


class UniversalPolicyTests(unittest.TestCase):
    def test_same_vacancy_matches_only_its_configured_profession(self):
        for role in ['QA Engineer','Product Designer','Python Developer','Бухгалтер','Повар']:
            with self.subTest(role=role):
                p=post(text=role+'\nCompany: Example\nRemote')
                self.assertNotEqual(assess(p,Rules(target_roles=(role,))).category,REJECTED)
                self.assertEqual(assess(p,Rules(target_roles=('Unrelated role',))).category,REJECTED)

    def test_no_default_profession_or_default_accept_all(self):
        self.assertEqual(assess(post(text='System Analyst\nAPI Kafka')).category,REJECTED)
        with self.assertRaises(ValueError): Rules.from_config({})

    def test_exclusion_is_user_defined_not_qa_hardcoded(self):
        qa=post(text='QA Engineer\nAPI, Kafka, SQL')
        self.assertNotEqual(assess(qa,Rules(target_roles=('QA Engineer',))).category,REJECTED)
        self.assertEqual(assess(qa,Rules(target_roles=('QA Engineer',),excluded_roles=('QA Engineer',))).category,REJECTED)

    def test_mentions_of_wanted_colleagues_do_not_reclassify_job(self):
        rules=Rules(target_roles=('Product Designer',))
        p=post(text='QA Engineer\nWork with Product Designer\nRequirements: Product Designer collaboration')
        self.assertEqual(assess(p,rules).category,REJECTED)

    def test_header_and_title_aliases(self):
        p=post(text='#vacancy #remote\nCompany: Example\nPosition: Senior Python-Developer\nResponsibilities: build services')
        self.assertEqual(assess(p,Rules(target_roles=('Python Developer',))).role_kind,'target')

    def test_literal_word_matching_avoids_rest_inside_interested(self):
        rules=Rules(target_roles=('Some role',),keywords=('REST',),allow_keyword_only=True)
        self.assertEqual(assess(post(text='Specialist\nInterested in this job?'),rules).category,REJECTED)

    def test_required_and_excluded_keywords(self):
        rules=Rules(target_roles=('Designer',),required_keywords=('Figma',),excluded_keywords=('unpaid',))
        for text,accepted in [('Designer\nFigma',True),('Designer\nSketch',False),('Designer\nFigma unpaid',False)]:
            self.assertEqual(assess(post(text=text),rules).category!=REJECTED,accepted)

    def test_keyword_only_requires_explicit_opt_in(self):
        rules=Rules(target_roles=('Designer',),keywords=('Figma',))
        p=post(text='Specialist\nFigma')
        self.assertEqual(assess(p,rules).category,REJECTED)
        self.assertEqual(assess(p,replace(rules,allow_keyword_only=True)).role_kind,'candidate')

    def test_policy_isolation_between_instances(self):
        stores=[Store(':memory:',rules=Rules(target_roles=(r,))) for r in ['QA Engineer','Designer']]
        try:
            for s in stores:
                s.ingest('demo',[],NOW)
                s.ingest('demo',[post(text='QA Engineer')],NOW)
            self.assertEqual([len(s.preview()) for s in stores],[1,0])
        finally:
            for s in stores: s.close()

    def test_matching_configuration_validation(self):
        for values in [{'target_roles':[]},{'target_roles':'Designer'},
                       {'target_roles':['Designer'],'allow_keyword_only':True},
                       {'target_roles':['Designer'],'exclude_typo':['A']},
                       {'target_roles':['   ']},{'target_roles':['Designer'],'allow_keyword_only':'false'}]:
            with self.subTest(values=values),self.assertRaises(ValueError): Rules.from_config({'matching':values})

    def test_explicit_target_survives_model_disagreement(self):
        p=post(text='QA Engineer\nDesign tests')
        a=assess(p,Rules(target_roles=('QA Engineer',)))
        self.assertNotEqual(refine(p,a,{'verdict':'no_match','duties':[]}).category,REJECTED)

    def test_unclear_model_result_retries_ambiguous_candidate(self):
        s=Store(':memory:',rules=Rules(target_roles=('QA Engineer',),keywords=('testing',),allow_keyword_only=True),review_enabled=True)
        try:
            s.ingest('demo',[],NOW)
            s.ingest('demo',[post(text='Specialist\nAutomated testing')],NOW)
            item=s.claim_review(0)
            self.assertEqual(s.finish_review(item,{'verdict':'unclear','duties':[]},1),'retry')
            self.assertEqual(s.preview(),[])
        finally: s.close()

    def test_card_keeps_link_salary_and_omits_unknown_wall(self):
        p=post(text='Product Designer в Example\nRemote, EU residents only\nSalary: €4000\nFigma')
        body=render(p,assess(p,Rules(target_roles=('Product Designer',),keywords=('Figma',))),NOW)
        self.assertIn('€4000',body)
        self.assertIn('Example',body)
        self.assertNotIn('не указано',body)
        self.assertTrue(body.endswith(p.original_url))

    def test_relative_private_profile_resolves_next_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.toml'
            path.write_text('[applications]\nprofile_dir="private/profile"\n')
            self.assertEqual(load_config(path)['applications']['profile_dir'],str((Path(tmp)/'private'/'profile').resolve()))

    def test_model_receives_requested_profession_not_fixed_family_list(self):
        response=Mock()
        response.read.return_value=json.dumps({'done':True,'message':{'content':json.dumps({'verdict':'match','role_quote':'QA Engineer','duties':[]})}}).encode()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        with patch('jobbot.localmodel.build_opener') as opener:
            opener.return_value.open.return_value=response
            classify(post(text='QA Engineer'),'example:model',Rules(target_roles=('QA Engineer',)))
            payload=json.loads(opener.return_value.open.call_args.args[0].data)
        data=json.loads(payload['messages'][1]['content'])
        self.assertEqual(data['policy']['target_roles'],['QA Engineer'])
        self.assertNotIn('systems_analysis',str(payload))

    def test_bot_identity_comes_from_token_and_optional_expectation(self):
        api=BotAPI('fake:test')
        api.call=Mock(side_effect=[{'username':'friend_jobs_bot','is_bot':True},{'url':''}])
        self.assertEqual(api.preflight()['username'],'friend_jobs_bot')

    def test_letters_allow_arbitrary_cv_facts_and_need_no_ollama(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            profile=fixture(directory)
            profile['facts'][0].update(id='pastry',keywords=['pastry'],en='I prepare pastry and plan ingredient orders.',ru='Готовлю выпечку и планирую закупку ингредиентов.')
            profile['facts'][1].update(id='quality',keywords=['quality'],en='I check product quality before service.',ru='Проверяю качество продукции перед подачей.')
            (directory/'profile.json').write_text(json.dumps(profile,ensure_ascii=False),encoding='utf-8')
            with patch('jobbot.applications.local_json') as model:
                packet=prepare(post(text='Pastry Chef at Example\nPastry quality'),directory,use_model=False)
                model.assert_not_called()
            self.assertIn('I prepare pastry',packet['letter'])
            self.assertNotIn('systems analyst',packet['letter'])
            self.assertNotIn('API',packet['letter'])
            self.assertNotIn('cv',packet)


if __name__=='__main__': unittest.main()

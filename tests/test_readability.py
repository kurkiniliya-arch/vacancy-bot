import json
import unittest
from unittest.mock import patch,Mock
from jobbot.filtering import assess,render,refine
from test_support import Store
from jobbot.localmodel import classify,ModelUnavailable
from test_bot import post,NOW

OUTSIDE='вне предварительного отбора'


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:',review_enabled=True)
        self.s.ingest('demo',[post()],NOW)

    def tearDown(self): self.s.close()

    def queue(self,text='System Analyst\nAPI Kafka'):
        self.s.ingest('demo',[post(2,text=text)],NOW)
        return self.s.claim_review(0)

    def test_model_runs_before_outbox_not_on_baseline(self):
        self.assertIsNone(self.s.claim_review(0))
        item=self.queue()
        self.assertEqual(self.s.preview(),[])
        self.assertEqual(self.s.finish_review(item,{'verdict':'match','duties':['API Kafka']},1),'queued')
        self.assertEqual(len(self.s.preview()),1)

    def test_timeout_keeps_explicit_target_and_defers_ambiguous(self):
        item=self.queue()
        self.assertEqual(self.s.finish_review(item,None,1),'queued')
        self.s.ingest('demo',[post(3,text='Аналитик\nУдалённо')],NOW)
        ambiguous=self.s.claim_review(1)
        self.assertEqual(self.s.finish_review(ambiguous,None,1),'retry')
        self.assertIsNone(self.s.claim_review(300))
        self.assertIsNotNone(self.s.claim_review(301))

    def test_model_does_not_drop_explicit_systems_analyst(self):
        item=self.queue()
        self.assertEqual(self.s.finish_review(item,{'verdict':'no_match','duties':[]},1),'queued')

    def test_model_excludes_ambiguous_wrong_specialization(self):
        item=self.queue('Аналитик\nРабота с требованиями, API')
        self.assertEqual(self.s.finish_review(item,{'verdict':'no_match','duties':[]},1),'rejected')
        self.assertEqual(self.s.preview(),[])

    def test_inference_result_for_edited_post_is_discarded(self):
        item=self.queue()
        self.s.ingest('demo',[post(2,text='QA Engineer\nAPI Kafka requirements')],NOW)
        self.assertEqual(self.s.finish_review(item,{'verdict':'match','duties':[]},1),'stale')
        self.assertEqual(self.s.preview(),[])

    def test_restart_retries_readonly_model_job_not_telegram_send(self):
        self.queue()
        self.s.recover_reviews()
        self.assertIsNotNone(self.s.claim_review(1))
        self.assertEqual(self.s.preview(),[])

    def test_cross_channel_reviews_send_only_one_card(self):
        self.s.ingest('other',[],NOW)
        url='https://example.org/jobs/same'
        self.s.ingest('demo',[post(2,vacancy_url=url)],NOW)
        old=self.s.claim_review(0)
        self.s.ingest('other',[post(2,source='other',vacancy_url=url)],NOW)
        self.assertEqual(self.s.finish_review(old,{'verdict':'match','duties':[]},1),'stale')
        current=self.s.claim_review(1)
        self.s.finish_review(current,{'verdict':'match','duties':[]},2)
        self.assertEqual(len(self.s.preview()),1)

class LocalClientTests(unittest.TestCase):
    def test_rejects_unfinished_output_and_does_not_follow_external_endpoints(self):
        response=Mock()
        response.read.return_value=json.dumps({'done':False,'message':{'content':'{}'}}).encode()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        with patch('jobbot.localmodel.build_opener') as opener:
            opener.return_value.open.return_value=response
            with self.assertRaises(ModelUnavailable): classify(post())
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'http://127.0.0.1:11434/api/chat')

    def test_hallucinated_text_does_not_reach_card(self):
        p=post()
        a=refine(p,assess(p),{'verdict':'match','duties':['Invented €9000 salary']})
        self.assertNotIn('€9000',render(p,a,NOW))


if __name__=='__main__': unittest.main()

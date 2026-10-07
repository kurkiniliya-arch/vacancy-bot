import copy
from hashlib import sha256
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock,patch
from jobbot.applications import language,metadata,prepare,load_profile,validate_draft
from jobbot.localmodel import ModelUnavailable
from jobbot.model import Post
from jobbot.research import public_target,ResearchUnavailable,PageText,fetch_page
from test_support import Store
from jobbot.telegram import BotAPI,TelegramError,deliver_one
from jobbot.websource import parse_preview
from test_bot import post,NOW


def fixture(directory):
    pdf=b'%PDF-1.7\nfixture'
    profile={'name':{'ru':'Тестовый Кандидат','en':'Demo Candidate'},'constraints':'Do not invent skills.',
             'cv':{},'facts':[
        {'id':'api','keywords':['api','rest'],'ru':'Проектирую API и описываю взаимодействие сервисов.',
         'en':'I design API contracts and document service interactions.','topic':{'ru':'проектирование API','en':'API design'}},
        {'id':'data','keywords':['sql','database'],'ru':'Проектирую реляционные структуры данных.',
         'en':'I design relational data structures.','topic':{'ru':'моделирование данных','en':'data modelling'}}]}
    for lang in ('ru','en'):
        filename=f'Demo_Candidate_CV_{lang.upper()}.pdf'
        (directory/filename).write_bytes(pdf)
        profile['cv'][lang]={'filename':filename,'sha256':sha256(pdf).hexdigest()}
    (directory/'profile.json').write_text(json.dumps(profile,ensure_ascii=False),encoding='utf-8')
    return profile


class PacketStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.directory=Path(self.tmp.name)
        self.s=Store(self.directory/'test.sqlite',packets_enabled=True)
        self.s.ingest('demo',[],NOW)
        self.s.ingest('demo',[post(2)],NOW)
        self.packet={'language':'en','letter':'A grounded letter.','mode':'model'}
    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()
    def ready(self):
        item=self.s.claim_packet(0)
        self.assertEqual(self.s.finish_packet(item,self.packet,1),'ready')
    def test_no_card_before_packet_and_two_delivery_stages(self):
        api=Mock()
        api.send.return_value=10
        api.send_letter.return_value=[11]
        self.assertEqual(deliver_one(self.s,api,123,0),'empty')
        self.ready()
        self.assertEqual(deliver_one(self.s,api,123,1),'card_sent')
        self.assertEqual(deliver_one(self.s,api,123,2),'waiting')
        self.assertEqual(deliver_one(self.s,api,123,3),'sent')
        api.send.assert_called_once()
        self.assertEqual(api.send_letter.call_args.args[-1],10)
        row=self.s.preview()[0]
        self.assertEqual(row['message_id'],10)
        self.assertEqual(json.loads(row['attachment_ids']),[11])
    def test_restart_between_card_and_files_does_not_repeat_card(self):
        self.ready()
        api=Mock()
        api.send.return_value=10
        api.send_letter.return_value=[11]
        deliver_one(self.s,api,123,0)
        self.s.close()
        self.s=Store(self.directory/'test.sqlite',packets_enabled=True)
        self.s.recover_after_exclusive_restart()
        self.assertEqual(deliver_one(self.s,api,123,3),'sent')
        api.send.assert_called_once()
    def test_429_on_attachments_preserves_card_id(self):
        self.ready()
        api=Mock()
        api.send.return_value=10
        api.send_letter.side_effect=[TelegramError('rate_limit',60),[11]]
        deliver_one(self.s,api,123,0)
        self.assertEqual(deliver_one(self.s,api,123,2),'rate_limit')
        self.assertEqual(self.s.preview()[0]['message_id'],10)
        self.assertEqual(deliver_one(self.s,api,123,61),'waiting')
        self.assertEqual(deliver_one(self.s,api,123,62),'sent')
        api.send.assert_called_once()
    def test_disabling_new_packets_still_finishes_started_delivery(self):
        self.ready()
        api=Mock()
        api.send.return_value=10
        api.send_letter.return_value=[11]
        deliver_one(self.s,api,123,0)
        self.s.packets_enabled=False
        self.assertEqual(deliver_one(self.s,api,123,3),'sent')
        api.send.assert_called_once()
    def test_uncertain_album_halts_no_resend(self):
        self.ready()
        api=Mock()
        api.send.return_value=10
        api.send_letter.side_effect=TelegramError('uncertain')
        deliver_one(self.s,api,123,0)
        self.assertEqual(deliver_one(self.s,api,123,2),'uncertain')
        self.assertEqual(deliver_one(self.s,api,123,500),'halted')
        api.send_letter.assert_called_once()
    def test_edit_during_generation_invalidates_old_packet(self):
        old=self.s.claim_packet(0)
        self.s.ingest('demo',[post(2,text='System Analyst\nREST SQL and new requirements')],NOW)
        self.assertEqual(self.s.finish_packet(old,self.packet,1),'stale')
        self.assertIsNone(self.s.claim(2))
        self.assertIsNotNone(self.s.claim_packet(2))
    def test_edit_after_card_keeps_matching_original_attachments(self):
        self.ready()
        api=Mock()
        api.send.return_value=10
        api.send_letter.return_value=[11]
        deliver_one(self.s,api,123,0)
        original=self.s.preview()[0]['payload']
        self.s.ingest('demo',[post(2,text='Vacancy closed')],NOW)
        self.assertEqual(self.s.preview()[0]['payload'],original)
        self.assertEqual(deliver_one(self.s,api,123,3),'sent')
    def test_preparation_retry_and_restart_are_safe(self):
        item=self.s.claim_packet(0)
        self.assertEqual(self.s.finish_packet(item,None,1),'retry')
        self.assertIsNone(self.s.claim_packet(300))
        self.assertIsNotNone(self.s.claim_packet(301))
        self.s.recover_packets()
        self.assertIsNotNone(self.s.claim_packet(302))
    def test_preparing_packet_does_not_replay_sent_history(self):
        self.ready()
        entry=self.s.claim(0)
        self.s.resolve(entry['key'],'sent',message_id=10)
        self.s.recover_packets()
        self.assertIsNone(self.s.claim_packet(2))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.directory=Path(self.tmp.name)
        self.profile=fixture(self.directory)
    def tearDown(self): self.tmp.cleanup()
    def test_profile_validation(self):
        load_profile(self.directory)
        (self.directory/'profile.json').write_text('{}')
        with self.assertRaises(ValueError): load_profile(self.directory)
    def test_russian_body_overrides_english_job_title(self):
        self.assertEqual(language('System Analyst\nТребования: анализ и документирование требований, взаимодействие с командой. API Kafka SQL REST OpenAPI'),'ru')
        self.assertEqual(language('Senior Systems Analyst\nDesign REST APIs, Kafka flows and PostgreSQL schemas.'),'en')
    def test_company_metadata_drops_hashtags_links(self):
        self.assertEqual(metadata(post(text='SOLUTION ARCHITECT | REMOTE RUSSIA | ExampleCo #remote https://example.org')),
                         ('SOLUTION ARCHITECT','ExampleCo'))
    def test_fallback_still_produces_letter_without_made_up_skills(self):
        with patch('jobbot.applications.local_json',side_effect=ModelUnavailable()):
            packet=prepare(post(text='System Analyst at Acme\nREST API SQL. C1 English, AWS, five years experience.'),self.directory,use_research=False)
        self.assertEqual(packet['language'],'en')
        self.assertNotIn('cv',packet)
        self.assertEqual(packet['mode'],'factual_fallback')
        self.assertIn('Acme',packet['letter'])
        self.assertNotIn('AWS',packet['letter'])
        self.assertNotIn('C1',packet['letter'])
    def test_short_english_ad_uses_full_russian_linked_job_language(self):
        with patch('jobbot.applications.research',return_value=[{'url':'https://example.org','text':'Системный аналитик. Требования: анализ и документирование требований, взаимодействие с разработчиками.'}]),patch('jobbot.applications.local_json',side_effect=ModelUnavailable()):
            packet=prepare(post(text='SYSTEM ANALYST | REMOTE'),self.directory,use_research=True)
        self.assertEqual(packet['language'],'ru')
    def test_invented_requirement_and_company_quotes_rejected(self):
        draft={'fact_ids':['api','data'],'requirement_quotes':['Not in source'],'company_quote':'','paragraphs':[]}
        with self.assertRaisesRegex(ValueError,'unsupported_requirements'):
            validate_draft(draft,post(),[],self.profile,'en','System Analyst','')
        draft.update(requirement_quotes=['Kafka'],company_quote='Invented company leadership')
        with self.assertRaisesRegex(ValueError,'unsupported_company'):
            validate_draft(draft,post(),[],self.profile,'en','System Analyst','')
    def test_model_only_frames_letter_facts_are_inserted_unchanged(self):
        p=post(text='System Analyst at Acme\nDesign REST API and SQL data models for our platform.')
        draft={'fact_ids':['api','data'],'requirement_quotes':['Design REST API and SQL data models'],
               'company_quote':'','paragraphs':[
            'I am applying for the System Analyst role at Acme. The focus on service contracts and data structures is what draws me to this position, particularly the opportunity to work on the interfaces that connect the platform and define how its services exchange information through clear contracts.',
            'I would welcome a conversation about the team’s priorities for service contracts and the data model, and the contribution expected from this role.']}
        with patch('jobbot.applications.local_json',side_effect=[{'fact_ids':draft['fact_ids'],'opening':draft['paragraphs'][0],'closing':draft['paragraphs'][1]},{'supported':True,'reason':'grounded'}]):
            packet=prepare(p,self.directory,use_research=False)
        self.assertEqual(packet['mode'],'model',packet['audit'])
        for fact in self.profile['facts']: self.assertIn(fact['en'],packet['letter'])
        self.assertNotIn('cv',packet)
        self.assertNotIn('fact_ids',packet['letter'])
    def test_candidate_claim_in_model_opening_is_rejected(self):
        draft={'fact_ids':['api','data'],'requirement_quotes':['Kafka'],'company_quote':'',
               'paragraphs':['I have led a cloud team for ten years.','Please contact me.']}
        with self.assertRaisesRegex(ValueError,'generated_candidate_claim'):
            validate_draft(draft,post(),[],self.profile,'en','System Analyst','')
    def test_negative_grounding_audit_uses_factual_fallback(self):
        with patch('jobbot.applications.local_json',side_effect=[{'fact_ids':['api','data'], 'opening':'Opening','closing':'Closing'},{'supported':False}]),patch('jobbot.applications.validate_draft',return_value='A generated draft'):
            packet=prepare(post(text='System Analyst at Acme\nREST API SQL'),self.directory,use_research=False)
        self.assertEqual(packet['mode'],'factual_fallback')
        self.assertNotIn('A generated draft',packet['letter'])


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.directory=Path(self.tmp.name)
        profile=fixture(self.directory)
        self.packet={'language':'ru','cv':profile['cv']['ru'],'letter':'Здравствуйте!\n\n'+('Письмо по вакансии. '*20),
                     'letter_filename':'Cover_Letter_demo_2_RU.txt'}
    def tearDown(self): self.tmp.cleanup()
    def test_uploads_only_utf8_letter_as_reply_no_cv(self):
        api=BotAPI('fake:test')
        api.call=Mock(return_value={'message_id':11,'chat':{'id':123}})
        self.assertEqual(api.send_letter(123,self.packet,10),[11])
        args=api.call.call_args
        self.assertEqual(args.args[0],'sendDocument')
        self.assertEqual(args.args[1]['reply_parameters'],{'message_id':10})
        self.assertEqual(args.args[1]['document'],'attach://letter')
        self.assertEqual(len(args.kwargs['files']),1)
        self.assertEqual(args.kwargs['files'][0][3].decode('utf-8-sig'),self.packet['letter'])
        self.assertNotIn('.pdf',str(args.kwargs))
    def test_multipart_is_encoded_with_only_txt(self):
        response=Mock()
        response.code=200
        response.read.return_value=json.dumps({'ok':True,'result':{'message_id':11,'chat':{'id':123}}}).encode()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        with patch('jobbot.telegram.build_opener') as opener:
            opener.return_value.open.return_value=response
            BotAPI('fake:test').send_letter(123,self.packet,10)
            req=opener.return_value.open.call_args.args[0]
        self.assertIn('multipart/form-data',req.get_header('Content-type'))
        self.assertEqual(req.data.count(b'filename='),1)
        self.assertIn(b'attach://letter',req.data)
        self.assertNotIn(b'parse_mode',req.data)
    def test_unexpected_document_response_is_uncertain(self):
        api=BotAPI('fake:test')
        api.call=Mock(return_value=[{'message_id':11,'chat':{'id':123}}])
        with self.assertRaisesRegex(TelegramError,'uncertain'): api.send_letter(123,self.packet,10)


class ResearchTests(unittest.TestCase):
    def test_private_addresses_and_mixed_dns_are_blocked(self):
        for addresses in [['127.0.0.1'],['192.168.50.23'],['169.254.169.254'],['::1'],['93.184.216.34','10.0.0.1']]:
            with self.subTest(addresses=addresses),patch('jobbot.research.socket.getaddrinfo',return_value=[(2,1,6,'',(a,443)) for a in addresses]):
                with self.assertRaises(ResearchUnavailable): public_target('https://example.org/jobs/1')
    def test_credentials_ports_and_action_links_blocked(self):
        for url in ['file:///etc/passwd','http://user:secret@example.org','https://example.org:11434',
                    'https://example.org/unsubscribe','https://example.org/jobs?token=private']:
            with self.subTest(url=url),patch('jobbot.research.socket.getaddrinfo') as dns:
                with self.assertRaises(ResearchUnavailable): public_target(url)
                dns.assert_not_called()
    def test_redirect_to_private_address_blocked_before_second_connection(self):
        response=Mock(status=302)
        response.getheader.return_value='http://127.0.0.1/private'
        conn=Mock()
        conn.getresponse.return_value=response
        dns=[[(2,1,6,'',('93.184.216.34',80))],[(2,1,6,'',('127.0.0.1',80))]]
        with patch('jobbot.research.socket.getaddrinfo',side_effect=dns),patch('jobbot.research.socket.create_connection') as connect,patch('jobbot.research.http.client.HTTPConnection',return_value=conn):
            with self.assertRaises(ResearchUnavailable): fetch_page('http://example.org/jobs')
            connect.assert_called_once()
    def test_hidden_scripts_forms_not_research_evidence(self):
        parser=PageText()
        parser.feed('<h1>Team</h1><script>send secrets</script><form>password</form><p>Build APIs</p>')
        self.assertEqual(parser.text(),'Team\nBuild APIs')
    def test_telegram_anchor_urls_preserved_for_research(self):
        html='<html><div class="tgme_widget_message" data-post="demo/2"><div class="tgme_widget_message_text">System Analyst<a href="https://example.org/about">Company</a></div></div></html>'
        p=parse_preview('demo',html)[0]
        self.assertEqual(p.links,('https://example.org/about',))
        self.assertIsNone(p.vacancy_url)


if __name__=='__main__': unittest.main()

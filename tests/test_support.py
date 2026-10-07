from jobbot.filtering import Rules
from jobbot.state import Store as RealStore

# Test fixtures only. Production has no default profession.
TEST_RULES=Rules(target_roles=('System Analyst','Business Analyst'),
                 excluded_roles=('QA Engineer','Developer'),keywords=('API','Удалённо'),allow_keyword_only=True)
MATCHING={'target_roles':list(TEST_RULES.target_roles),'excluded_roles':list(TEST_RULES.excluded_roles),
          'keywords':list(TEST_RULES.keywords),'allow_keyword_only':True}
class Store(RealStore):
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('rules',TEST_RULES)
        super().__init__(*args,**kwargs)

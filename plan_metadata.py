"""表示用のプラン名と、日付付き公式案内。送信許可には使用しない。"""
from datetime import date


POLICY_SOURCE = 'https://learn.chatgpt.com/docs/pricing'
POLICY_REVIEWED_ON = date(2026, 10, 20)
POLICY_MAX_AGE_DAYS = 30

# API区分をそのまま扱い、価格・倍率・workspaceの契約条件は推測しない。
PLAN_LABELS = {
    'free': 'Free', 'go': 'Go', 'plus': 'Plus', 'pro': 'Pro',
    'prolite': 'Pro（prolite）', 'promax': 'Pro（promax）', 'team': 'Team',
    'business': 'Business', 'self_serve_business_prolite': 'Business（prolite）',
    'self_serve_business_usage_based': 'Business（従量）', 'ent26': 'Enterprise（ent26）',
    'enterprise_cbp_automation': 'Enterprise（automation）',
    'enterprise_cbp_usage_based': 'Enterprise（従量）', 'enterprise': 'Enterprise',
    'edu': 'Edu', 'edu_plus': 'Edu Plus', 'edu_pro': 'Edu Pro',
}


def policy_is_current(today=None):
    age = ((today or date.today()) - POLICY_REVIEWED_ON).days
    return 0 <= age <= POLICY_MAX_AGE_DAYS


def window_policy(plan, minutes, *, today=None):
    """True=案内で制限あり、False=対象外、None=断定不可。API実測が優先。"""
    if not policy_is_current(today) or minutes != 300:
        return None
    # 公式Pricingの「Proは現在5時間枠なし」という確認日付き案内。
    # prolite/promaxとの商品対応は未確認のため継承しない。
    # API枠の欠落から推測した権利情報ではなく、実測があればそちらを優先する。
    if plan == 'pro':
        return False
    if plan == 'plus':
        return True
    # Business/Edu/Enterpriseはseatやflexible pricingの条件を区別できない。
    # 週次も「適用される場合がある」ため、欠落だけで有無を確定しない。
    return None

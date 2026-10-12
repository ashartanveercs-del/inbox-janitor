import pytest
from factories import msg

from inbox_janitor.rescue import authenticated, judge

SERVER = "mx.example.com"
TRUSTED = ("billing@flour.example.com", "@bank.example.com")


def spam(sender="billing@flour.example.com", auth: str | None = None, **kw):
    headers = {"Authentication-Results": auth} if auth is not None else {}
    return msg(sender=sender, labels=("SPAM",), headers=headers, **kw)


def passing(domain="flour.example.com", server=SERVER):
    return (f"{server};\r\n       dkim=pass header.i=@{domain} header.s=s1 header.b=AbC;\r\n"
            f"       spf=pass (example: domain of x designates 192.0.2.1 as permitted sender) "
            f"smtp.mailfrom=bounce@{domain};\r\n"
            f"       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from={domain}")


def test_a_trusted_authenticated_sender_is_rescued():
    verdict = judge(spam(auth=passing()), TRUSTED, SERVER)
    assert verdict.rescue and verdict.reason == "trusted"


def test_a_domain_pattern_covers_its_senders():
    m = spam(sender="alerts@bank.example.com", auth=passing("bank.example.com"))
    assert judge(m, TRUSTED, SERVER).rescue


def test_an_untrusted_sender_stays_in_spam_even_when_authenticated():
    m = spam(sender="promo@ovens.example.com", auth=passing("ovens.example.com"))
    verdict = judge(m, TRUSTED, SERVER)
    assert not verdict.rescue and verdict.reason == "not_trusted"


@pytest.mark.parametrize("auth", [
    None,                                                             # no verdict at all
    f"{SERVER}; dkim=fail header.d=flour.example.com; dmarc=fail header.from=flour.example.com",
    f"{SERVER}; spf=pass smtp.mailfrom=billing@flour.example.com",    # SPF says nothing of From
    f"{SERVER}; dkim=pass header.d=bulk-mailer.example.com",          # not aligned with From
    f"{SERVER}; dmarc=pass header.from=evil.example.com",             # a different From domain
])
def test_a_trusted_address_without_authentication_is_not_rescued(auth):
    """The From header is trivially forged: trusting the address alone would let anyone pull
    a phishing message into the inbox by claiming to be your supplier."""
    verdict = judge(spam(auth=auth), TRUSTED, SERVER)
    assert not verdict.rescue and verdict.reason == "unauthenticated"


def test_a_verdict_from_some_other_server_is_ignored():
    """Anyone can write an Authentication-Results header into their own message; only the
    receiving server's verdict counts."""
    forged = passing(server="mx.attacker.example.com")
    assert not judge(spam(auth=forged), TRUSTED, SERVER).rescue


def test_dkim_alone_counts_when_aligned_with_the_from_domain():
    auth = f"{SERVER}; dkim=pass header.d=flour.example.com header.s=s1"
    assert authenticated(spam(sender="billing@mail.flour.example.com", auth=auth), SERVER)


def test_dkim_identity_with_an_at_sign_is_understood():
    auth = f"{SERVER}; dkim=pass header.i=@flour.example.com"
    assert authenticated(spam(auth=auth), SERVER)


def test_relaxed_alignment_accepts_the_parent_domain_but_never_a_bare_tld():
    parent = f"{SERVER}; dkim=pass header.d=example.com"
    tld = f"{SERVER}; dkim=pass header.d=com"
    assert authenticated(spam(auth=parent), SERVER)
    assert not authenticated(spam(auth=tld), SERVER)


def test_a_sibling_domain_is_not_aligned():
    auth = f"{SERVER}; dkim=pass header.d=notflour.example.com"
    assert not authenticated(spam(auth=auth), SERVER)


def test_the_server_id_comparison_ignores_case_and_a_version_number():
    auth = passing(server="MX.Example.COM 1")
    assert authenticated(spam(auth=auth), SERVER)

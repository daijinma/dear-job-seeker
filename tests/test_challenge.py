"""A verification page (slider, captcha, "confirm you are human") served with HTTP 200 instead of the data is a Blocked,
whichever platform it comes from; a page that parses, or one that failed to parse and says nothing of the kind, is not
(djs.providers.base._watched). The same rule for the list and for the detail."""
import html
import inspect
import json
import threading
from pathlib import Path

import httpx
import pytest

from djs import results
from djs.providers import base
from djs.providers.base import (Blocked, Delisted, FetchError, LoginRequired, ParseError, Provider, StructureError,
                                check_http, need)
from djs.providers.bytedance import Bytedance
from djs.providers.ciic import Ciic
from djs.providers.kq36 import Kq36
from djs.providers.mohrss import Mohrss
from djs.providers.nowcoder import NowCoder
from provider_fakes import client

SEARCH = {"keyword": "客服"}
PAGES = {   # what a site says instead of the data, as far as the marks go
    "slider": "<html><title>安全验证</title><body><div id='nc_1_wrapper'>请按住滑块，拖动到最右边</div></body></html>",
    "visit": "<html><title>访问验证</title><body><p>您的访问行为异常，请完成验证后继续访问</p></body></html>",
    "human": "<html><body><h1>人机验证</h1><button>点击验证</button></body></html>",
    "captcha-script": "<script src='https://turing.captcha.qcloud.com/TCaptcha.js'></script>",
    "geetest": "<script src='https://static.geetest.com/static/js/gt.0.4.9.js'></script>",
    "cloudflare": "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>",
    "english": "<html><body><h1>Please verify you are human to continue</h1></body></html>",
    "aliyun-waf": "<html><script>window.location.href='/_____tmd_____/punish?x5secdata=abc'</script></html>",
}
LISTING = [{"acb200": "J1", "aca112": "客服专员", "aab004": "甲公司", "acb22a": "岗位职责：发送验证码、处理 captcha 工单"}]
MOHRSS_PAGE = f'<input name="findjoblist" value="{html.escape(json.dumps(LISTING, ensure_ascii=False))}">'
NOWCODER_PAGE = {"code": 0, "data": {"datas": [{"id": 1, "jobName": "验证码审核员 (captcha)"}]}}
CIIC_ROW = ('<li class="position-info-item" uuid="ab12"><div class="job-name ellipsis"><a href="x" title="客服">客服</a></div>'
            '<span class="job-salary ellipsis" title="8-10K">8-10K</span><h3><a title="甲公司">甲公司</a></h3></li>')
CIIC_LIST = f"<title>职位列表-中智招聘网</title><div class='main-inner-container'>{CIIC_ROW}</div>"
CIIC_DETAIL = '<h4>职位描述</h4><p class="describe-content">登录需要验证码 captcha，请按流程处理</p>'
KQ_FIXTURE = (Path(__file__).parent / "fixtures" / "kq36_list.html").read_bytes()
KQ_LIST = KQ_FIXTURE.replace("示例护士".encode(), "验证码审核员".encode())   # a job that says 验证码 in its title
KQ_DETAIL = '<div class="job-sec-text">岗位职责：处理验证码与 captcha 工单</div>'.encode()


def fetch(prov, **answer):
    return prov.fetch(client(**answer), dict(SEARCH), 1)


@pytest.mark.parametrize("shape", PAGES)
@pytest.mark.parametrize("prov, answer", [
    (Mohrss(), lambda body: dict(text=body)),                       # HTML: the listing input is not there
    (NowCoder(), lambda body: dict(text=body)),                     # JSON: the body is not JSON
    (NowCoder(), lambda body: dict(text=json.dumps({"code": 9999, "msg": body}, ensure_ascii=False))),   # JSON: a refusal in a business code
    (NowCoder(), lambda body: dict(text=json.dumps({"code": 9999, "msg": body}))),   # the same, its Chinese \u-escaped
], ids=["html-mohrss", "json-nowcoder", "json-business-code", "json-escaped"])
def test_a_verification_page_instead_of_the_data_is_a_block(prov, answer, shape):
    with pytest.raises(Blocked) as caught:
        fetch(prov, **answer(PAGES[shape]))
    assert isinstance(caught.value.__cause__, StructureError)   # what the provider said is kept behind it
    assert caught.value.retry_after is None
    assert results.classify(f"Blocked: {caught.value}")[0] == "blocked"   # read as "被网站暂时限制", not "页面变了"


@pytest.mark.parametrize("call", [
    lambda: Ciic().detail(client(text=PAGES["slider"]), {"uuid": "ab12"}),
    lambda: Kq36().detail(client(text=PAGES["visit"]), {"id": "9000001"}),
    lambda: Kq36().fetch(client(text=PAGES["human"]), dict(SEARCH), 1),
], ids=["ciic-detail", "kq36-detail", "kq36-list"])
def test_an_html_verification_page_is_a_block_on_the_detail_and_on_the_list(call):
    with pytest.raises(Blocked):
        call()


def test_a_page_that_parses_is_never_looked_at_for_marks():
    jobs = fetch(Mohrss(), text=MOHRSS_PAGE)   # the job's own text says 验证码 and captcha
    assert [j["aca112"] for j in jobs] == ["客服专员"] and "验证码" in jobs[0]["acb22a"]
    assert fetch(NowCoder(), body=NOWCODER_PAGE)[0]["jobName"] == "验证码审核员 (captcha)"
    ciic = Ciic()
    assert ciic.fetch(client(text=CIIC_LIST), SEARCH, 1)[0]["uuid"] == "ab12"
    assert "验证码" in ciic.detail(client(text=CIIC_DETAIL), {"uuid": "ab12"})["description"]
    kq36 = Kq36()
    assert kq36.fetch(client(content=KQ_LIST), dict(SEARCH), 1)[0]["title"] == "验证码审核员"
    assert "验证码" in kq36.detail(client(content=KQ_DETAIL), {"id": "9000001"})["description"]


@pytest.mark.parametrize("prov, answer, error", [
    (Mohrss(), dict(text="<html><body>职位列表改版了</body></html>"), StructureError),
    (Mohrss(), dict(text=""), StructureError),
    (NowCoder(), dict(text="Service Unavailable"), ParseError),
    (NowCoder(), dict(body={"code": 5, "msg": "boom"}), StructureError),
    (NowCoder(), dict(body={"code": 0, "data": {}}), StructureError),
], ids=["html", "empty", "not-json", "business-code", "no-list"])
def test_a_page_that_cannot_be_read_and_says_nothing_of_a_verification_stays_a_structure_error(prov, answer, error):
    with pytest.raises(error) as caught:
        fetch(prov, **answer)
    assert not isinstance(caught.value, Blocked)


def test_a_detail_that_cannot_be_read_stays_a_parse_error_without_marks():
    with pytest.raises(ParseError):
        Ciic().detail(client(text="<html><body>下架了</body></html>"), {"uuid": "ab12"})


def test_a_large_page_is_a_page_that_changed_even_when_it_says_验证码():
    """A real site's page carries a login box with 验证码 on every page; it is far larger than a verification page."""
    big = "<html><body>" + "手机验证码登录 " * 5000 + "</body></html>"
    assert len(big) > base.CHALLENGE_MAX
    with pytest.raises(StructureError) as caught:
        fetch(Mohrss(), text=big)
    assert not isinstance(caught.value, Blocked)
    with pytest.raises(Blocked):   # the same words on a small page are the verification
        fetch(Mohrss(), text=big[:2000])


def test_a_json_list_whose_keys_changed_is_a_structure_error_even_when_the_jobs_say_验证码():
    """A renamed key leaves the job texts in the body, and jobs of a tech site talk of 验证码 and captcha all the time:
    a JSON body that holds job texts is far larger than a refusal, which is a line or two."""
    posts = [{"id": i, "title": "后端", "description": "负责登录模块：短信验证码与图形 captcha 的接入。" * 10} for i in range(8)]
    body = {"code": 0, "data": {"posts": posts}}
    assert base.CHALLENGE_MAX_JSON < len(json.dumps(body, ensure_ascii=False)) < base.CHALLENGE_MAX
    with pytest.raises(StructureError) as caught:
        fetch(Bytedance(), body=body)
    assert not isinstance(caught.value, Blocked)
    with pytest.raises(Blocked):   # the refusal of the same site, as short as such answers are
        fetch(Bytedance(), body={"code": 9999, "message": "请先完成安全验证", "data": None})


def test_http_refusals_and_login_walls_are_what_they_were():
    with pytest.raises(Blocked, match="HTTP 403"):
        fetch(Mohrss(), text=PAGES["slider"], status=403)   # the status decides first, as before
    with pytest.raises(httpx.HTTPStatusError):
        fetch(Mohrss(), text=PAGES["slider"], status=500)    # and so does any other: not a verification page


class Plain(Provider):
    name, label = "plain", "示例"

    def fetch(self, client, payload, page):
        if payload.get("offline"):   # no request at all: whatever an earlier call saw is not this error's page
            need({}, "items")
        return need(json.loads(check_http_text(client.get("https://site.test/list"))), "items")

    def detail(self, client, job):
        return need(json.loads(check_http_text(client.get("https://site.test/detail"))), "jd", typ=str)


def check_http_text(r):
    check_http(r)
    return r.text


def test_what_an_earlier_call_saw_is_not_blamed_for_the_next_error():
    plain = Plain()
    with pytest.raises(Blocked):
        plain.fetch(client(text=json.dumps({"hint": "captcha"})), {}, 1)   # nothing under "items": a verification
    assert base._checked.get() is None   # and the page is not kept
    with pytest.raises(StructureError) as caught:
        plain.fetch(client(text="{}"), {"offline": True}, 1)
    assert not isinstance(caught.value, Blocked)
    check_http(client(text=PAGES["slider"]).get("https://site.test/list"))   # a page read outside any fetch or detail
    with pytest.raises(StructureError) as caught:
        plain.fetch(client(text="{}"), {"offline": True}, 1)   # is not this fetch's page either
    assert not isinstance(caught.value, Blocked)


def test_every_fetch_and_detail_a_provider_class_defines_is_watched_once():
    class Mine(Mohrss):
        def fetch(self, client, payload, page):
            return super().fetch(client, payload, page)

    assert Mine.fetch.challenge_watched and Plain.fetch.challenge_watched and Plain.detail.challenge_watched
    assert not hasattr(Provider.fetch, "challenge_watched")   # the contract itself is left as it is
    assert Plain.fetch.__wrapped__.__name__ == "fetch" and not hasattr(Plain.fetch.__wrapped__, "challenge_watched")
    assert list(inspect.signature(Mohrss.fetch).parameters) == ["self", "client", "payload", "page"]   # as it was written
    assert list(inspect.signature(Plain.detail).parameters) == ["self", "client", "job"]
    assert Plain().has_detail and not Mohrss().has_detail
    with pytest.raises(Blocked):   # an override that calls its parent is converted once, not twice
        Mine().fetch(client(text=PAGES["slider"]), dict(SEARCH), 1)


class Odd(Provider):
    name, label = "odd", "示例"

    def fetch(self, client, payload, page):
        check_http(client.get("https://site.test/list"))   # a verification page was read ...
        raise payload["raise"]                              # ... and then something else went wrong


@pytest.mark.parametrize("error", [ValueError("x"), FetchError("other"), Delisted("off"), LoginRequired("HTTP 401"),
                                   httpx.ReadTimeout("slow"), KeyError("k")], ids=lambda e: type(e).__name__)
def test_what_is_not_a_structure_error_passes_through_untouched(error):
    with pytest.raises(type(error)) as caught:
        Odd().fetch(client(text=PAGES["slider"]), {"raise": error}, 1)
    assert caught.value is error


def test_platforms_that_fail_at_the_same_moment_do_not_read_each_others_page():
    """Every platform has its own worker thread: what one thread read last is not what another one failed on."""
    class Gate(Provider):
        name = "gate"

        def fetch(self, client, payload, page):
            r = client.get("https://site.test/list")
            check_http(r)
            both_have_read.wait(5)   # the other thread has read its page too: a shared "last page" would be its page now
            return need(json.loads(r.text), "items")

    both_have_read = threading.Barrier(2, timeout=5)
    got = {}

    def run(key, text):
        try:
            Gate().fetch(client(text=text), {}, 1)
        except Exception as e:   # noqa: BLE001 - what each thread ends with is the point
            got[key] = e

    threads = [threading.Thread(target=run, args=("slider", json.dumps({"hint": "captcha"}))),
               threading.Thread(target=run, args=("changed", "{}"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert isinstance(got["slider"], Blocked)
    assert isinstance(got["changed"], StructureError) and not isinstance(got["changed"], Blocked)

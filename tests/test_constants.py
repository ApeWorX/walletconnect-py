from walletconnect import constants


def test_default_relay_url():
    assert constants.DEFAULT_RELAY_URL == "wss://relay.walletconnect.org"


def test_tags_are_unique():
    tags = []
    for opts in (*constants.PAIRING_RPC_OPTS.values(), *constants.SIGN_RPC_OPTS.values()):
        tags.extend(o.tag for o in (opts.req, opts.res, opts.reject, opts.auto_reject) if o)

    assert len(tags) == len(set(tags))


def test_response_tag_follows_request_tag():
    for opts in (*constants.PAIRING_RPC_OPTS.values(), *constants.SIGN_RPC_OPTS.values()):
        assert opts.res.tag == opts.req.tag + 1


def test_spec_values():
    propose = constants.SIGN_RPC_OPTS["wc_sessionPropose"]
    assert (propose.req.tag, propose.req.ttl) == (1100, 300)
    assert propose.reject
    assert propose.reject.tag == 1120
    assert propose.auto_reject
    assert propose.auto_reject.tag == 1121
    assert constants.SIGN_RPC_OPTS["wc_sessionRequest"].req.tag == 1108
    assert constants.SIGN_RPC_OPTS["wc_sessionPing"].req.ttl == 30
    assert constants.SIGN_RPC_OPTS["wc_sessionDelete"].req.ttl == 86400

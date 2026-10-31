"""Verify two distinct human approvals against an explicit trusted key registry.

No built-in reviewers/keys; M03 assistant role authorization is not recognized.
The caller provisions the identity-to-key mapping outside this candidate service.
"""
import hmac
import hashlib
from types import MappingProxyType
from app.services.governance_fact_contracts import WriterError,canonical,digest,instant


class ReviewAuthority:
    def __init__(self,principals):
        saved={}
        for identity,record in principals.items():
            if not isinstance(identity,str) or not identity or type(record) is not dict or set(record)!={'kind','key'} or record['kind'] not in {'human','assistant'} or type(record['key']) is not bytes or len(record['key'])<32:raise WriterError('REVIEW_REGISTRY_INVALID')
            saved[identity]=MappingProxyType(dict(record))
        self.principals=MappingProxyType(saved)

    def verify(self,context_hash,reviews,*,now):
        error='TWO_HUMAN_APPROVALS_REQUIRED'
        if type(reviews) is not list or len(reviews)!=2:raise WriterError(error)
        seen=set();packets=[]
        for review in reviews:
            if type(review) is not dict or set(review)!={'reviewer_id','decision','signed_at','valid_until','context_hash','evidence_hash','signature','evidence'}:raise WriterError(error)
            identity=review['reviewer_id'];principal=self.principals.get(identity)
            if not principal or principal['kind']!='human' or identity in seen or review['decision']!='approve' or review['context_hash']!=context_hash:raise WriterError(error)
            if type(review['evidence']) is not bytes or not review['evidence'] or digest(review['evidence'])!=review['evidence_hash']:raise WriterError(error)
            packet={k:v for k,v in review.items() if k not in {'signature','evidence'}}
            expected=hmac.new(principal['key'],canonical(packet).encode('utf-8'),hashlib.sha256).hexdigest()
            if not isinstance(review['signature'],str) or not hmac.compare_digest(expected,review['signature']):raise WriterError(error)
            if not instant(review['signed_at'])<=now<instant(review['valid_until']):raise WriterError(error)
            packets.append({**packet,'signature':review['signature']});seen.add(identity)
        return packets

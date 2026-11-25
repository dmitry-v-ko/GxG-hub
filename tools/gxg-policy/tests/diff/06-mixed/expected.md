<!-- gxg-policy-diff -->
## Policy diff (gxg-policy)

**widening: 4**, narrowing: 1.

Widening needs the `widening-approved` label from a code owner (GR-10, AC-M2-04).

### Widening

| tenant | kind | change | details |
| --- | --- | --- | --- |
| `t-alpha` | approval_rule | decision_relaxed | rule=`msg`, base=`require_approval`, head=`auto_approve` |
| `platform` | webhook | added | id=`audit-sink` |
| `t-alpha` | role | added | principal=`oidc:https://idp.example.invalid:1`, role=`approver` |
| `platform` | service | tenant_added | id=`demo`, service_tenant=`t-beta` |

### Narrowing

| tenant | kind | change | details |
| --- | --- | --- | --- |
| `t-alpha` | grant | removed | subject=`agent:t-alpha:hermes`, action=`collect`, resource=`kb:kb-main/collection/public` |

# agentmsg — usage examples

## Minimal send/receive

```python
from agentmsg import send, Mailbox

alice = Mailbox("alice")
send("bob", "alice", "cobalt-postage: first test message")
print(alice.pop())
```

Expected output:

```
Message(sender='bob', body='cobalt-postage: first test message')
```

## Subscribing to a mailbox

```python
from agentmsg import subscribe, send

def printer(msg):
    print(f"[{msg.sender}] {msg.body}")

subscribe("alice", printer)
send("bob", "alice", "cobalt-postage: subscribed delivery")
```

## Notes

- Capacity defaults to 64 messages per mailbox (bounded FIFO; oldest is dropped
  when full).
- `Mailbox.deliver` is the low-level hook; `send` is the ergonomic helper.
- The `cobalt-postage` codeword above is a livetest marker — docs-recall probes
  look for it to prove the RAG pipeline indexed this file.

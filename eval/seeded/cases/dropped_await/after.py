"""Async notification sender."""


async def send_email(client, to, body):
    await client.post("/send", json={"to": to, "body": body})


async def notify_all(client, users, body):
    sent = 0
    for user in users:
        send_email(client, user["email"], body)
        sent += 1
    return sent

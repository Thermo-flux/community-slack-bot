# Build & ship

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/shipping

A ship is a version of your project that you hand in for review. Someone who has never seen your project should be able to tell what changed and check the result themselves.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## The shipping checklist

1. **It runs.** It starts without crashing, and the repository has a README that says how to run it.
2. **Someone can see it.** A demo or proof anyone can open: a hosted link, a video, or clear photos for hardware.
3. **The hours match.** Your tracked time is for this project and this version.
4. **Your journal is up to date.** A few sentences for each work session: what you did and why.
5. **You reflect.** Say what you built yourself, where AI helped, and what is still rough.

Then send it in with the [submission steps](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/submitting). Not sure where to start? The [first project guide](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/first-project) walks through it.

## Examples

Two examples and one journal entry show what a good ship looks like. Read the real one first.

### A real project: Poorup

This one is real. [Poorup](https://poorup.tech) is a browser-based, real-time multiplayer property-trading game that Jerry built. Its server decides every move instead of trusting the player's browser. It also recovers a player who drops and reconnects, and it has automated tests that run on every change. The code is public: [see its code](https://github.com/jeremy341/Poorup).

| Part | Detail |
| --- | --- |
| Time | About 100 hours |
| Rating | 18.8 out of 20 (Jerry's own, as an illustration: no LOADOUT review has happened yet) |
| Ore | 100 hours × 18.8 = 1,880 Ore |
| What that buys | For example the enclosed 3D printer (1,000 Ore) and a Bambu Lab A1 Mini (500 Ore) |

A project rated this high is rare, and the prices are still planned, not final. The point is the shape of the reward: it comes from real hours and from what other builders think of the result.

### An invented example: a sensor board

This is an invented sample, here to show the shape of a good ship. Read the real project above first. It is not an approved project and the numbers are made up.

**Tunnel monitor: a USB temperature and humidity logger.** *Tracks: Hardware, plus a little Systems for the log format.*

> **What this version adds.** A custom two-layer board around a small microcontroller and a temperature and humidity sensor, plus firmware that logs a reading every second to onboard flash and dumps the log over USB.

*Why this opens well: it says what exists and what it does, in two sentences.*

**How it works.** The firmware reads the sensor over I2C, stores each reading in a ring buffer in flash, and answers a "dump" command over USB serial. The hard parts were wear-leveling the flash and getting readings on time while USB was busy.

*Why this helps a reviewer: it names the technical problems, so they know where to look for depth.*

**Evidence.**

- Source and design files: `firmware/`, `board/` (schematic, layout, Gerbers), `README.md` with build and flash steps.
- Parts list: 14 lines, each with a quantity and a supplier. The board and firmware are mine. The sensor module is bought.
- Demo: a 40-second video showing the board warming in a hand and the log appearing on screen.
- Tests: a bench comparison against a reference thermometer over one hour, with the error graphed. A note that humidity was only checked at room level.

*Why this works: each claim has something to open, and the gap in testing is stated, not hidden.*

**My contribution and sources.** I designed the board and wrote the firmware. I used the vendor's sensor driver unchanged and wrote my own flash layout. An AI assistant helped me draft the serial command parser; I rewrote the error handling after reading it and tested it with bad input.

*Why this helps: a reviewer can see what is yours and what the tool did, so nothing needs guessing.*

**Known limits.** No enclosure yet. Flash wear-leveling is tested for a day, not months.

### A sample journal entry

This invented entry shows the kind of detail that helps. See [Tracking & evidence](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/tracking) for how journals connect to recorded work.

> **Day 6: the logs have gaps.** Readings were missing whenever I dumped the log over USB. The USB handler was blocking the sensor read for up to 40 ms. I tried a faster I2C clock first. It made the gaps shorter, but they didn't go away. Then I moved the sensor read to a timer interrupt and let the USB code only copy bytes. Gaps gone over a 10-minute test with 20 dumps. Commit `a3f9c1`, plot in `notes/day6-gaps.png`. Next: flash wear.

It says what went wrong, what failed, what fixed it and how it was checked. It points to evidence. Compare it with "worked on firmware", which tells a reviewer nothing.

## Shipping again

You can ship the same project as many times as you like. Each ship covers only the hours since the previous one, and it's rated on its own. Hours that were already paid are never paid again, and nothing earlier changes.

A new ship needs journal entries for its new hours and a short note saying what changed. A ship with nothing new to show isn't reviewed. If a ship is rejected, fix it and send it again: the hours stay in the same window and aren't lost.

For example, a first ship of 20 hours rated 9 pays 180 Ore. Three weeks later, a second ship of 12 new hours rated 13 pays 156 Ore. The second one doesn't touch the first.

The same implementation or hours can't be rewarded twice. [Project review](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review) covers how a ship is rated.

# Build your first project

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/first-project

This is the path for a first project. It takes a few evenings, and you don't need to know anyone.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## The five steps

1. Pick a technical capability and the track or tracks it belongs to.
2. Build it. Keep a journal as you go.
3. Package a result someone else can run or look at, with the files behind it.
4. Say what this version adds, what you did yourself, what is still rough, and what help you had (AI included).
5. Send the version and its evidence for review.

## Before you start

- Pick one track (Tools, Systems, Compute or Hardware). You can add another later.
- Make a folder for the project and keep it in one place.
- Set up version control so every change is saved. [Git and GitHub setup](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/git) walks through it.
- Set up time tracking before you write any code, so the first hours are counted. See [time tracking setup](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/time-setup).

## If you get stuck

Ask in #loadout-help. Say what you tried, what happened and what you expected. Keep your journal open while you ask: it is the fastest way to show where you are.

# Git and GitHub setup

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/git

Git saves every version of your project, so you can show what changed and go back when something breaks. GitHub keeps a copy online, so a reviewer can open it.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## 1. Install and introduce yourself

Install Git from the official site for your system. Then tell Git your name and an email address. Use the email you use for your GitHub account:

```bash
git config --global user.name "Your name"
git config --global user.email "you@example.com"
```

## 2. Start a repository

In your project folder, start the repository and save the first version:

```bash
git init
git add .
git commit -m "Start the project"
```

## 3. Make a daily habit

After each work session, check what changed, then save it with a short message that says what you did:

```bash
git status
git add .
git commit -m "Add the temperature log"
```

## 4. Keep build files out of Git

Some files should never be saved: installed packages, build output and secrets such as passwords or keys. Put their names in a `.gitignore` file in the project folder. Never put a key or password in a repository, even for a moment.

## 5. Put it on GitHub

Create an empty repository on GitHub, then connect your folder to it and send your saved versions up:

```bash
git remote add origin https://github.com/your-name/your-project.git
git push -u origin main
```

Your repository is then public or private, as you choose. A reviewer needs to be able to open the code, so make sure it is public when you submit.

# Project types

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/project-types

Every project has the same shipping checklist, but each kind of project has its own proof. Start with the one that fits yours.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## Software

Before you ship software, check that you can answer yes to each of these.

- **Source:** Is the code in a repository someone can open, with a clear license?
- **Run it:** Can a stranger run it from a clean checkout using only your README? List the versions that matter (language, runtime, OS).
- **Show it:** Is there a short demo, a recording, a terminal session or a screenshot of real output?
- **Prove it:** Do tests, benchmarks or measurements back up the claim you are making? A nice screenshot alone does not show that a runtime, a tool or an algorithm works.
- **Limits:** Does the README say what does not work yet?
- **Your part:** Is it clear which code is yours, which came from a library and which came from a tutorial or an AI tool?

## Hardware

For hardware, check these.

- **Design files:** Schematic, board layout, CAD or enclosure files, in their source format and as a viewable export.
- **Firmware:** The code that runs on the device, with how to build and flash it.
- **Parts list:** Every part, with the quantity and where you got it. Mark anything you designed yourself and anything you bought ready-made.
- **Photos:** The real device, not only a render. Show it powered and doing something.
- **Demo:** A short video or log of it working, ideally with the numbers it measures or the thing it controls.
- **Tests:** What you checked and how. A bench measurement against a known reference counts. So does an honest "it failed here".
- **Limits:** Say what is simulated, what is unfinished and what you haven't tested. Do not present those as a working physical result.

## Web apps

- **Demo:** a link anyone can open, not only you on your own machine.
- **Run it:** the README lists the versions you need, and a stranger can start it from a clean copy.
- **Show what you built:** a short recording of the main use, plus a note on what you wrote and what came from a library.

## Mobile apps

- **Demo:** a recording of the app on a real phone, or a test build someone can install.
- **Run it:** the README says which platform and tools you used.
- **Show what you built:** the screens you designed and the parts that hold data or talk to a server.

## Games

- **Demo:** a playable build, or a video of the game running from start to finish.
- **Run it:** the README covers the engine version and any assets you did not make.
- **Show what you built:** your scripts, levels or systems, and which art and music came from elsewhere, with its licence.

# Time tracking setup

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/time-setup

How tracked time will be set up for coding and for hardware. The tools are planned, not confirmed.

Your tracked hours decide what your ship is worth, so each project needs a tracker from the first day. Two tools are planned for LOADOUT. Neither is confirmed yet, so treat the steps below as the plan, not as instructions that will work today.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## Planned: Hackatime for code

Hackatime is planned for coding work. The plan is to install it in your editor, then check that your hours show against the right project before you write any code.

## Planned: Lapse for hardware

Lapse is planned for hardware and other work that is not code: soldering, assembling, printing and testing. The plan is to record a session when you start it and stop it when you finish.

## Either way

- Name the project the same way every time, so the hours land in one place.
- Check the hours each week against your journal. If they don't match, fix them before you ship.
- Hours that cannot be explained are not counted. See [what reviewers check](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/submitting#reviewers).

# Submitting and reviewers

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/submitting

How to send a ship in, what the submission needs, and what reviewers check before they rate it.

Submitting is the step where your ship leaves your hands. Reviewers then check it, and rate it. The rules for the rating are on [project review](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review). This page is about what you send and what they look at first.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## What to send in

Send the version and its evidence. A good submission has:

- a title and one line on what it is,
- the track or tracks it belongs to, and why,
- what this version adds (for a first ship, the whole project; for a later one, only the new work),
- how it works, with the one or two hard problems you solved,
- the evidence: links to the source, the demo, the tests and any measurements,
- your own part, and what teammates did,
- where you used AI, and what you checked yourself,
- what is still rough or untested.

## A submission outline

Use something like this as a starting point. Short is fine. Specific is better.

1. **Title and one line.** What it is, in plain words.
2. **Track or tracks.** Which fields the work belongs to, and why.
3. **What this version adds.** For a first ship, the whole project. For a later one, only the new work.
4. **How it works.** A paragraph or a diagram. Name the one or two hard problems you solved.
5. **Evidence.** Links to the source or design files, the demo, the tests and the measurements.
6. **My contribution.** What you built, what you reused, what teammates did.
7. **AI and sources.** Where you used AI, what you checked yourself, and what you borrowed.
8. **Known limits.** What is rough, missing or untested.

## What reviewers check first

Reviewers try to open your files and run or inspect your result. They ask whether the project fits LOADOUT, whether the evidence backs the claims, and then how strong the work is across Originality, Technical Depth, Execution and Documentation. [Project review](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review) explains the stages.

They look for the hard part you solved and the proof that it works. A short, honest submission with solid evidence beats a long one that only describes a result.

## Credit and AI disclosure

- Name every library, driver, tutorial, design and AI tool that shaped the project, and respect their licenses.
- Say what you did beyond them. Using a library is not the same as writing it.
- If AI helped, say where, and show what you understood and checked yourself. AI use alone does not rule a project out. [AI & team contributions](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/ai-teams) has the full expectations.
- On a team, each person describes their own part. The team's hours are not awarded to everyone.

# Tracking & evidence

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/tracking

How recorded time, your journal and your files fit together, without counting work twice.

Recorded time is one piece of evidence. Your journal and your files explain what that time produced and which part is yours.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## Tracked, journaled and approved work

| Record | What it's for |
| --- | --- |
| Tracked work | Shows when you worked. |
| Journal | Explains the work, the decisions, the experiments and the progress. |
| Approved work | The part a reviewer can verify and accept. |

These can differ. A reviewer may need to ask you something, or may approve only the part of a submission that the evidence supports.

Use Hackatime to record coding and Lapse for eligible hardware and other non-code work. Your journal connects the recording to what you built. The LOADOUT account integrations are listed under [program status](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand); that is separate from the availability of the tracking tools themselves.

## What a useful journal says

Say what changed, why you chose that approach, what failed and how you checked the result. Link the commits, measurements, photos or demos.

"Worked on the project" tells a reviewer nothing. "Compared two buffer layouts and measured their memory use under the same workload" explains a decision that evidence can back up.

### Useful evidence

Different projects need different proof: source changes, terminal output, benchmark graphs, CAD images, photos of the device or a recorded test.

Keep secrets, access tokens and other people's personal details out of public evidence.

## No double counting

Use each stretch of tracked work once. If one session supports several journal entries, split it. Don't claim the whole session in each.

Team members record their own contributions. A later version covers only new work, not hours already approved for an earlier ship.

## When evidence has gaps

Say so honestly. More recorded hours don't fix missing files or unclear authorship. A reviewer should tell you what evidence is needed; see [review feedback](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review#feedback).

# Project review

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review

A team member checks your ship is real, then other builders rate it from 0 to 20. That rating is how many Ore each hour pays.

Review answers three questions: does the project fit LOADOUT, is what you submitted real, and how good is the technical work you can show? The answer to the last one is a number from 0 to 20, and it decides what your hours are worth.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## What happens to a ship

| Step | Who | The question |
| --- | --- | --- |
| Check | A team member | Does the technical core belong here, and do the files, journal and hours back up what you submitted? |
| Rating | Other builders | How strong is the work you showed? |
| Payout | The system | Ore and Track XP are added. The team can stop a payout if something looks wrong. |

A project can be useful and still sit outside LOADOUT's focus. A project that fits can also need more evidence before it moves on. If the check says no, you get a reason and a way to fix it, and you can ship again.

## How other builders rate it

Reviewers look at four things:

| Thing | The question |
| --- | --- |
| Originality | What's new in how you built it, or in the limits you worked within? |
| Technical Depth | What hard problem did you solve, and how well do you understand it? |
| Execution | Does it work for what it's meant to do? |
| Documentation | Could another builder understand it, check it or reproduce it? |

The rating scale uses written descriptions: **0** means nothing there or not your own work, **5** is rough but honest, **10** is solid, **15** is great, and **20** is outstanding. Your final rating is how many Ore each approved hour pays. The four questions above give reviewers a basis for that decision.

Twenty is the top of the scale for outstanding work. [Ore, XP & levels](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/progress) shows what the rating pays.

Peer review depends on builders reviewing each other's work. The required review count and deadlines are listed among the [remaining decisions](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand), so this guide doesn't assign a review quota yet.

## Cheating

Fake or inflated hours, copied work, fake evidence, and swapping scores with friends are fraud. Fraud is a permanent ban, and everything you earned or bought with it is taken back. There's no second chance, and it's the reason the rest can be generous: there's no limit on hours or projects, because we trust real hours.

## AI use and team projects

Reviewers need to see what you built yourself. Declare AI help, borrowed code, dependencies and each person's part.

[AI & team contributions](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/ai-teams) covers what's expected.

## Feedback and revisions

Good feedback names the decision, the work it covers, and anything missing or worth improving. A request for changes isn't a final no.

If you ship again, say what's new and add the evidence you were asked for. A new ship is rated on its own, for the hours since your last one. Submitting the same work twice won't get a different award, because the same hours are never paid twice.

## Questions about a decision

Ask the team for a second look if you think a rating missed something. Explain which decision you question and point to the evidence. The formal appeal limits, timing and score-change policy are still being settled.

Keep the review reference and say which decision you mean. **[#loadout-help](https://app.slack.com/client/E09V59WQY1E/C0C6XBLM0M7)** can clear things up, but it can't promise an outcome.

# AI & team contributions

Source: https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/ai-teams

Make clear who did what, and what help you had.

LOADOUT is meant to work for people using AI and people working in teams, while crediting what each builder actually contributes.

[Program status →](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/eligibility#where-things-stand)

## AI help

AI help is allowed. Say where you used it, and explain the work, the decisions and the checks that you understand and did yourself.

There is no published numerical AI limit. Reviewers need to see your own contribution and the checks you made; a percentage from an older plan or another program isn't a LOADOUT rule.

## Borrowed work and dependencies

List your dependencies, reused code, tutorials, designs and other sources. Say what you changed or built beyond them, and follow their licenses and credit requirements.

Using a library isn't the same as writing it. Review looks at the technical contribution you can show.

## Team projects

Each member keeps their own tracked work and journal. Describe who did what, and what proof backs each person's part.

| On a team | For each person |
| --- | --- |
| One repository or device can hold everyone's work. | Each builder is credited for their own approved part. |
| Members may work in different fields. | Their Track XP splits can differ. |
| The result is shared. | The team's total hours aren't awarded to every member. |

## What to say in a ship

State your role, the work you did, the help you had and what you checked. Include a clear AI note where it applies.

If roles overlap, explain how you worked together instead of claiming the same work twice. [Tracking & evidence](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/tracking) and [Project review](https://loadout-ysws-git-feature-all-changes-jerry-team1.vercel.app/docs/review) show how evidence and authorship fit together.

# JobApplicationAgent
Automation job application agent from monolith (Level 1) to robust, fault-tolerant (Level 2) and self-improving (Level 3)
-------------------------------------------------------------------------------------------------------------------------
#####
-----------------------------------
A Single Agent for Job Application
-----------------------------------
Automating a job application system, leveraging a single, monolithic agent- implementing it by leveraging Fractal Chain of Thought framework (FCoT)
Transitioning to a robust, maintainable and scalable multi-agent in the next iteration and finally a self-improving agent in later iterations.

-------------------------------------------
The Work Flow for the Job Application Agent
-------------------------------------------
-	Resume Intake and Validation:
o	Receiving the resume and ensuring that it can be utilized for one of the roles based on policy documents
o	Identify missing info
####
-	Resume Analysis and Check for a Specific Role:
o	Interacting with Job Boards, provided links and pasted JD to analyze if the resume is appropriate for the role
o	Securely handle integrations, parsing varied API responses and gracefully handle network errors should be expected
####
-	Confidence Score- Approval or Denial:
o	Apply internal business logic to provide a percentage as confidence score- 80%+ for approval and denail for 79% and below.
####
-	Compliance Review:
o	Auditing the process to ensure that it adheres with all relevant regulations
####
-	Optional Resume Modification:
o	Modify the resume to suit the role if confidence score is less than 80%
####
-	Custom Cover Letter Generation for a Specific Role:
o	Generate a cover letter for respective roles based on resume provided with first person narrative
####
-	Application
o	Swipe to apply for the job
--------------------------------------------------------------------------------------------------------------------------------------
####
Leveraging the FCoT Framework:
-------------------------------
-	Leveraging FCoT’ approach and pattern to build the agent out
-	First by crafting a prompt that captures the principles of FCoT. The prompt will act as the job application agent’s internal operating system. It will provide a formal structure for the mission of the job application agent, its constraints and its reasoning. It will act as law governing the agent’s actions. It will ensure there is no “lost in the middle” incidents. With leveraging the FCoT pattern, the agent will be able to recall instructions and critical constraints defined in the prompt and ensure there is no drift in its goal/objective as it performs its functions.
--------------------------------------------------------------------------------------------------------------------------------------
####
------------------------------------------------------
Implementing the 2 primary elements in FCoT Framework:
------------------------------------------------------
-	Instruction Contract (IC): The agent’s immutable source of truth. It defines the agent’s mission, the deliverables it needs to execute and the guardrails and constraints not to breach.
-   Recursive Loop: It defines the agent’s active thinking process- its reasoning. It forces the agent to follow through with established planned actions, carry them out and validate as it progresses against what IC dictates.
------------------------------------------------------------------------------------------------------------------------------------
####
-----------------------------
Designing a Monolithic Agent:
-----------------------------
-	The logic, tools and state management will be centrally managed by a single agent. The job application agent is to follow its internal FCoT prompt, sequentially invoking the right tools at the right time to move
-	Execute the job application workflow. The agent will be composed of 3 fundamental components below:

1.	The FCoT Reasoning Core: The prompt will serve as its brain- it will serve as its reasoning
2.	State Management: The Runner and SessionService in Google ADK (Orchestrator) will manage session and state- status of the job application agent historically
3.	The Tools: Integration of tools that will enable the interaction of the job application agent externally.



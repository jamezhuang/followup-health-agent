def merge_observations(message, facts, observations):
    updated = dict(facts)

    for observation in observations:
        if (
            observation.evidence not in message
            or observation.value not in observation.evidence
        ):
            raise ValueError("Observation is not grounded in latest input")

        previous = updated.get(observation.name, "")

        if (
            observation.operation == "append"
            and previous
            and observation.name not in {"age", "duration"}
        ):
            values = previous.split("；")

            if observation.value not in values:
                values.append(observation.value)

            value = "；".join(values)
        else:
            value = observation.value

        if len(value) > 1600:
            raise ValueError("Fact exceeds storage limit")

        updated[observation.name] = value

    return updated


def format_reply(result):
    sections = [result.summary]

    sections.extend(f"{claim.text} [{claim.evidence_id}]" for claim in result.claims)

    if result.follow_up_questions:
        sections.append("\n".join(result.follow_up_questions))

    return "\n\n".join(sections)

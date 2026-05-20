# NOTE: The `ai` app is currently a thin namespace — real AI logic lives in
# `emails.services` (`call_claude`, `generate_ai_reply`, `process_ai_reply_mode`)
# and `emails.views` (`ai_training_*` endpoints). Reserved here in case we
# split AI out into its own module later. Do not add views here yet.

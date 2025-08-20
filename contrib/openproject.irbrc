IRB.conf[:USE_MULTILINE] = false
IRB.conf[:PROMPT_MODE]   = :SIMPLE
IRB.conf[:SAVE_HISTORY]  = nil
IRB.conf[:HISTORY_FILE]  = nil

# Be conservative with Reline in non-interactive contexts
begin
  if defined?(Reline)
    Reline.output_modifier_proc = nil
    Reline.completion_proc      = nil
    Reline.prompt_proc          = nil
  end
rescue => e
  puts "IRB Reline config error: #{e.message}"
end

puts "IRBRC loaded"



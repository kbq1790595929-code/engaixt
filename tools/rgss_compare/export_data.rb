# Local adapter: load the user's original MTool resources, never game scripts.
require 'json'
require 'zlib'
require 'digest'

module RPG
  def self.const_missing(name)
    const_set(name, Class.new do
      def self.const_missing(child)
        const_set(child, Class.new(self))
      end
      def method_missing(name, *args)
        key = "@#{name}"
        return instance_variable_get(key) if args.empty? && instance_variable_defined?(key)
        super
      end
      def respond_to_missing?(name, include_private = false)
        instance_variable_defined?("@#{name}") || super
      end
    end)
  end
end

%w[Table Color Tone].each do |name|
  # Native RGSS primitives do not expose their binary payload as Ruby ivars.
  Object.const_set(name, Class.new { def self._load(raw); allocate; end })
end

RGSS_VERSION = case File.extname(ARGV[1]).downcase
               when '.rgssad' then '1.0.0'
               when '.rgss2a' then '2.0.0'
               else '3.0.0'
               end
$isMRI = true
def doMToolEvals; nil; end
resource_dir, archive_path, output_path = ARGV
load File.join(resource_dir, 'CLoader.rb')
load File.join(resource_dir, 'rgssad.rb')

def load_marshal(raw)
  loop do
    begin
      return Marshal.load(raw)
    rescue ArgumentError => error
      match = error.message.match(/undefined class\/module ([A-Za-z0-9_:]+)/)
      raise unless match
      current = Object
      match[1].split('::').reject(&:empty?).each do |part|
        current = current.const_defined?(part, false) ? current.const_get(part, false) : current.const_set(part, Class.new)
      end
    end
  end
end

def valid_text(text)
  candidate = text.dup.force_encoding('UTF-8')
  return candidate if candidate.valid_encoding?
  # Fail explicitly rather than compare strings with replacement characters.
  text.encode('UTF-8', 'Windows-31J')
end

def event_lists(obj, output = [], seen = {})
  return output if seen[obj.__id__]
  seen[obj.__id__] = true
  if obj.is_a?(Array)
    if obj.any? { |item| item.is_a?(RPG::EventCommand) }
      # This is the original MTool event exporter, not an EngAixt decoder.
      commands = obj.select { |item| item.is_a?(RPG::EventCommand) }
      CLoader.eventCommandListProcObj(commands).each do |command|
        code = command['code']
        params = command['parameters']
        if code == 401 && params[0].is_a?(String)
          output << valid_text(params[0])
        end
      end
    end
    obj.each { |item| event_lists(item, output, seen) }
  elsif obj.is_a?(Hash)
    obj.each { |key, value| event_lists(key, output, seen); event_lists(value, output, seen) }
  elsif !obj.is_a?(Numeric) && !obj.is_a?(Symbol)
    obj.instance_variables.each { |key| event_lists(obj.instance_variable_get(key), output, seen) }
  end
  output
end

archive = RGSSAD_MTool.new(archive_path)
archive.decrypt
entries = archive.instance_variable_get(:@data_imfs)
# MTool's archive enumeration closes its stream; reopen only for payload reads.
archive.instance_variable_set(:@file, File.open(archive_path, 'rb'))
out = {archive: File.basename(archive_path), archive_sha256: Digest::SHA256.file(archive_path).hexdigest,
       ruby: RUBY_DESCRIPTION, entries: [], scripts: []}
entries.each do |entry|
  raw = archive.read_data(entry[:pos], entry[:size], entry[:key])
  name = valid_text(entry[:filename]).tr('\\', '/')
  record = {name: name, sha256: Digest::SHA256.hexdigest(raw), size: raw.bytesize}
  if name.match?(/\AData\/.*\.(rxdata|rvdata|rvdata2)\z/i)
    obj = load_marshal(raw)
    if File.basename(name).match?(/\AScripts\./i)
      $RGSS_SCRIPTS = obj
      obj.each_index do |index|
        script_name, source = CLoader.extract_script_data(index)
        next unless source
        out[:scripts] << {index: index, name: valid_text(script_name), source: valid_text(source),
                          source_sha256: Digest::SHA256.hexdigest(source)}
      end
    else
      CLoader.clearTexts
      $enableProcTrsStrings = false
      record[:raw_strings] = CLoader.getAllStrings(obj, [], nil, true).map { |text| valid_text(text) }
      CLoader.clearTexts
      $enableProcTrsStrings = true
      record[:non_message_strings] = CLoader.getAllStrings(obj, [], nil, true).map { |text| valid_text(text) }
      record[:messages] = CLoader.getAllEvStringArr.map { |text| valid_text(text) }
      record[:event_scripts] = CLoader.getAllScriptArr.map { |text| valid_text(text) }
      record[:comments] = CLoader.getAllCommentArr.map { |text| valid_text(text) }
      record[:event_message_lines] = event_lists(obj)
    end
  end
  out[:entries] << record
end
archive.close
File.write(output_path, JSON.pretty_generate(out), encoding: 'UTF-8')

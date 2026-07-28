#include "ShaderInfoSchema.h"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <limits>
#include <unordered_set>

namespace infernux
{
namespace
{
enum class TokenKind : uint8_t
{
    Identifier,
    String,
    Number,
    LeftBrace,
    RightBrace,
    LeftBracket,
    RightBracket,
    LeftParen,
    RightParen,
    Equals,
    Comma,
    Semicolon,
    End,
    Invalid,
};

struct Token
{
    TokenKind kind = TokenKind::Invalid;
    std::string text;
    ShaderSourceLocation begin;
    ShaderSourceLocation end;
};

bool IsIdentifierStart(char value)
{
    const unsigned char byte = static_cast<unsigned char>(value);
    return std::isalpha(byte) != 0 || value == '_';
}

bool IsIdentifierContinue(char value)
{
    const unsigned char byte = static_cast<unsigned char>(value);
    return std::isalnum(byte) != 0 || value == '_' || value == '/' || value == '.' || value == '-';
}

class Lexer final
{
  public:
    explicit Lexer(std::string_view source, size_t startOffset = 0) : m_source(source)
    {
        while (m_cursor < startOffset && m_cursor < m_source.size())
            Advance();
    }

    [[nodiscard]] Token Next()
    {
        SkipTrivia();
        Token token;
        token.begin = Location();
        if (AtEnd()) {
            token.kind = TokenKind::End;
            token.end = token.begin;
            return token;
        }

        const char value = Peek();
        switch (value) {
        case '{':
            return Punctuation(TokenKind::LeftBrace);
        case '}':
            return Punctuation(TokenKind::RightBrace);
        case '[':
            return Punctuation(TokenKind::LeftBracket);
        case ']':
            return Punctuation(TokenKind::RightBracket);
        case '(':
            return Punctuation(TokenKind::LeftParen);
        case ')':
            return Punctuation(TokenKind::RightParen);
        case '=':
            return Punctuation(TokenKind::Equals);
        case ',':
            return Punctuation(TokenKind::Comma);
        case ';':
            return Punctuation(TokenKind::Semicolon);
        case '"':
            return String();
        default:
            break;
        }

        if (IsIdentifierStart(value))
            return Identifier();
        if (std::isdigit(static_cast<unsigned char>(value)) != 0 ||
            ((value == '-' || value == '+') && std::isdigit(static_cast<unsigned char>(Peek(1))) != 0))
            return Number();

        token.kind = TokenKind::Invalid;
        token.text.push_back(value);
        Advance();
        token.end = Location();
        return token;
    }

  private:
    [[nodiscard]] bool AtEnd() const noexcept
    {
        return m_cursor >= m_source.size();
    }

    [[nodiscard]] char Peek(size_t lookahead = 0) const noexcept
    {
        const size_t index = m_cursor + lookahead;
        return index < m_source.size() ? m_source[index] : '\0';
    }

    [[nodiscard]] ShaderSourceLocation Location() const noexcept
    {
        return {m_cursor, m_line, m_column};
    }

    void Advance()
    {
        if (AtEnd())
            return;
        if (m_source[m_cursor++] == '\n') {
            ++m_line;
            m_column = 1;
        } else {
            ++m_column;
        }
    }

    void SkipTrivia()
    {
        while (!AtEnd()) {
            if (std::isspace(static_cast<unsigned char>(Peek())) != 0) {
                Advance();
                continue;
            }
            if (Peek() == '/' && Peek(1) == '/') {
                while (!AtEnd() && Peek() != '\n')
                    Advance();
                continue;
            }
            if (Peek() == '/' && Peek(1) == '*') {
                Advance();
                Advance();
                while (!AtEnd() && !(Peek() == '*' && Peek(1) == '/'))
                    Advance();
                if (!AtEnd()) {
                    Advance();
                    Advance();
                }
                continue;
            }
            break;
        }
    }

    [[nodiscard]] Token Punctuation(TokenKind kind)
    {
        Token token;
        token.kind = kind;
        token.begin = Location();
        token.text.push_back(Peek());
        Advance();
        token.end = Location();
        return token;
    }

    [[nodiscard]] Token Identifier()
    {
        Token token;
        token.kind = TokenKind::Identifier;
        token.begin = Location();
        const size_t start = m_cursor;
        while (!AtEnd() && IsIdentifierContinue(Peek()))
            Advance();
        token.text = std::string(m_source.substr(start, m_cursor - start));
        token.end = Location();
        return token;
    }

    [[nodiscard]] Token Number()
    {
        Token token;
        token.kind = TokenKind::Number;
        token.begin = Location();
        const size_t start = m_cursor;
        if (Peek() == '-' || Peek() == '+')
            Advance();
        while (std::isdigit(static_cast<unsigned char>(Peek())) != 0)
            Advance();
        if (Peek() == '.') {
            Advance();
            while (std::isdigit(static_cast<unsigned char>(Peek())) != 0)
                Advance();
        }
        if (Peek() == 'e' || Peek() == 'E') {
            Advance();
            if (Peek() == '-' || Peek() == '+')
                Advance();
            while (std::isdigit(static_cast<unsigned char>(Peek())) != 0)
                Advance();
        }
        token.text = std::string(m_source.substr(start, m_cursor - start));
        token.end = Location();
        return token;
    }

    [[nodiscard]] Token String()
    {
        Token token;
        token.kind = TokenKind::String;
        token.begin = Location();
        Advance();
        while (!AtEnd() && Peek() != '"') {
            if (Peek() == '\\' && Peek(1) != '\0') {
                Advance();
                const char escaped = Peek();
                switch (escaped) {
                case 'n':
                    token.text.push_back('\n');
                    break;
                case 't':
                    token.text.push_back('\t');
                    break;
                default:
                    token.text.push_back(escaped);
                    break;
                }
                Advance();
                continue;
            }
            token.text.push_back(Peek());
            Advance();
        }
        if (Peek() == '"') {
            Advance();
        } else {
            token.kind = TokenKind::Invalid;
        }
        token.end = Location();
        return token;
    }

    std::string_view m_source;
    size_t m_cursor = 0;
    uint32_t m_line = 1;
    uint32_t m_column = 1;
};

bool IsPropertyType(std::string_view value)
{
    static const std::unordered_set<std::string> Types = {"Float", "Float2", "Float3", "Float4",
                                                          "Color", "Int",    "Mat4",   "Texture2D"};
    return Types.find(std::string(value)) != Types.end();
}

bool IsValueType(std::string_view value)
{
    return IsPropertyType(value) && value != "Texture2D";
}

bool IsInterpolation(std::string_view value)
{
    return value == "Smooth" || value == "Flat" || value == "NoPerspective" || value == "Centroid";
}

std::string Lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
    return value;
}

class Parser final
{
  public:
    explicit Parser(std::string_view source) : m_source(source), m_lexer(source)
    {
        Advance();
    }

    [[nodiscard]] ShaderInfoDocument Parse()
    {
        while (m_current.kind != TokenKind::End) {
            if (m_current.kind == TokenKind::Identifier &&
                (m_current.text == "ShaderInfo" || m_current.text == "ShadingModelInfo")) {
                const Token declarationToken = m_current;
                m_document.foundDeclaration = true;
                m_document.kind =
                    m_current.text == "ShaderInfo" ? ShaderInfoKind::Shader : ShaderInfoKind::ShadingModel;
                m_document.declaration.begin = declarationToken.begin;
                Advance();
                if (!Consume(TokenKind::LeftBrace, "expected '{' after shader declaration")) {
                    m_document.declaration = {declarationToken.begin, m_current.end};
                    return m_document;
                }
                ParseFields();
                m_document.declaration.end = m_previous.end;
                Validate();
                DetectDuplicateDeclarations();
                return m_document;
            }
            Advance();
        }
        return m_document;
    }

  private:
    void Advance()
    {
        m_previous = m_current;
        m_current = m_lexer.Next();
    }

    bool Consume(TokenKind kind, std::string message)
    {
        if (m_current.kind == kind) {
            Advance();
            return true;
        }
        Error(m_current, std::move(message));
        return false;
    }

    void Error(const Token &token, std::string message)
    {
        m_document.diagnostics.push_back({ShaderInfoDiagnosticSeverity::Error, token.begin, std::move(message)});
    }

    void Warning(const Token &token, std::string message)
    {
        m_document.diagnostics.push_back({ShaderInfoDiagnosticSeverity::Warning, token.begin, std::move(message)});
    }

    std::optional<Token> Scalar(std::string_view field)
    {
        if (m_current.kind != TokenKind::Identifier && m_current.kind != TokenKind::String &&
            m_current.kind != TokenKind::Number) {
            Error(m_current, "expected a value for " + std::string(field));
            return std::nullopt;
        }
        Token value = m_current;
        Advance();
        return value;
    }

    std::optional<bool> Boolean(std::string_view field)
    {
        auto token = Scalar(field);
        if (!token)
            return std::nullopt;
        const std::string value = Lower(token->text);
        if (value == "on" || value == "true" || value == "yes")
            return true;
        if (value == "off" || value == "false" || value == "no")
            return false;
        Error(*token, std::string(field) + " expects On or Off");
        return std::nullopt;
    }

    void ParseFields()
    {
        while (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            if (m_current.kind != TokenKind::Identifier) {
                Error(m_current, "expected a ShaderInfo field name");
                Advance();
                continue;
            }
            const Token key = m_current;
            Advance();
            if (key.text == "Name") {
                if (auto value = Scalar(key.text))
                    m_document.name = value->text;
            } else if (key.text == "Version") {
                Error(key, "ShaderInfo Version has been removed");
                SkipUnknownValue();
            } else if (key.text == "ShadingModel") {
                if (auto value = Scalar(key.text))
                    m_document.shadingModel = value->text;
            } else if (key.text == "Surface") {
                if (auto value = Scalar(key.text))
                    m_document.surfaceType = Lower(value->text);
            } else if (key.text == "Queue") {
                ParseQueue();
            } else if (key.text == "Cull") {
                if (auto value = Scalar(key.text))
                    m_document.cullMode = Lower(value->text);
            } else if (key.text == "DepthWrite") {
                if (auto value = Scalar(key.text))
                    m_document.depthWrite = Lower(value->text);
            } else if (key.text == "DepthTest") {
                if (auto value = Scalar(key.text))
                    m_document.depthTest = Lower(value->text);
            } else if (key.text == "Blend" || key.text == "BlendMode") {
                if (auto value = Scalar(key.text))
                    m_document.blendMode = Lower(value->text);
            } else if (key.text == "PassTag") {
                if (auto value = Scalar(key.text))
                    m_document.passTag = Lower(value->text);
            } else if (key.text == "Stencil") {
                if (auto value = Scalar(key.text))
                    m_document.stencil = value->text;
            } else if (key.text == "AlphaClip") {
                if (auto value = Scalar(key.text))
                    m_document.alphaClip = value->text;
            } else if (key.text == "CastShadows") {
                m_document.castShadows = Boolean(key.text);
            } else if (key.text == "ReceiveShadows") {
                m_document.receiveShadows = Boolean(key.text);
            } else if (key.text == "Hidden") {
                m_document.hidden = Boolean(key.text);
            } else if (key.text == "Properties") {
                ParseProperties();
            } else if (key.text == "Inputs") {
                ParseVaryings(m_document.inputs, "Inputs");
            } else if (key.text == "Outputs") {
                ParseVaryings(m_document.outputs, "Outputs");
            } else if (key.text == "Imports") {
                ParseList(m_document.imports, "Imports");
            } else if (key.text == "Capabilities") {
                ParseList(m_document.capabilities, "Capabilities");
            } else if (key.text == "Resources") {
                ParseResources();
            } else if (key.text == "PushConstants") {
                ParsePushConstants();
            } else if (key.text == "Entry") {
                ParseEntry(key);
            } else {
                Warning(key, "unknown ShaderInfo field '" + key.text + "'");
                SkipUnknownValue();
            }
            if (m_current.kind == TokenKind::Semicolon)
                Advance();
        }
        Consume(TokenKind::RightBrace, "unterminated ShaderInfo declaration");
    }

    void ParseResources()
    {
        if (!Consume(TokenKind::LeftBrace, "expected '{' after Resources"))
            return;
        std::unordered_set<std::string> names;
        while (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            const Token begin = m_current;
            if (m_current.kind != TokenKind::Identifier || m_current.text != "Texture2D") {
                Error(m_current, "Resources currently supports Texture2D declarations");
                SkipToPropertyBoundary();
                continue;
            }
            ShaderInfoResource resource;
            resource.type = m_current.text;
            Advance();
            if (m_current.kind != TokenKind::Identifier) {
                Error(m_current, "expected a resource name");
                SkipToPropertyBoundary();
                continue;
            }
            resource.name = m_current.text;
            const Token nameToken = m_current;
            Advance();
            if (!names.insert(resource.name).second)
                Error(nameToken, "duplicate resource '" + resource.name + "'");
            resource.source = {begin.begin, m_previous.end};
            m_document.resources.push_back(std::move(resource));
            if (m_current.kind == TokenKind::Semicolon)
                Advance();
        }
        Consume(TokenKind::RightBrace, "unterminated Resources block");
    }

    void ParsePushConstants()
    {
        if (m_document.pushConstants) {
            Error(m_current, "ShaderInfo may declare PushConstants only once");
            SkipUnknownValue();
            return;
        }
        ShaderInfoPushConstants constants;
        if (m_current.kind != TokenKind::Identifier) {
            Error(m_current, "PushConstants requires an instance name");
            return;
        }
        constants.instanceName = m_current.text;
        Advance();
        if (!Consume(TokenKind::LeftBrace, "expected '{' after PushConstants instance name"))
            return;
        std::unordered_set<std::string> names;
        while (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            const Token begin = m_current;
            if (m_current.kind != TokenKind::Identifier || !IsValueType(m_current.text)) {
                Error(m_current, "expected a supported PushConstants value type");
                SkipToPropertyBoundary();
                continue;
            }
            ShaderInfoConstant field;
            field.type = m_current.text;
            Advance();
            if (m_current.kind != TokenKind::Identifier) {
                Error(m_current, "expected a PushConstants field name");
                SkipToPropertyBoundary();
                continue;
            }
            field.name = m_current.text;
            const Token nameToken = m_current;
            Advance();
            if (!names.insert(field.name).second)
                Error(nameToken, "duplicate PushConstants field '" + field.name + "'");
            field.source = {begin.begin, m_previous.end};
            constants.fields.push_back(std::move(field));
            if (m_current.kind == TokenKind::Semicolon)
                Advance();
        }
        Consume(TokenKind::RightBrace, "unterminated PushConstants block");
        if (constants.fields.empty())
            Error(m_previous, "PushConstants requires at least one field");
        m_document.pushConstants = std::move(constants);
    }

    void ParseQueue()
    {
        const auto value = Scalar("Queue");
        if (!value)
            return;
        int parsed = 0;
        const auto result = std::from_chars(value->text.data(), value->text.data() + value->text.size(), parsed);
        if (result.ec != std::errc{} || result.ptr != value->text.data() + value->text.size()) {
            Error(*value, "Queue expects an integer");
            return;
        }
        m_document.renderQueue = parsed;
    }

    void ParseProperties()
    {
        if (!Consume(TokenKind::LeftBrace, "expected '{' after Properties"))
            return;
        std::unordered_set<std::string> names;
        while (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            const Token begin = m_current;
            if (m_current.kind != TokenKind::Identifier || !IsPropertyType(m_current.text)) {
                Error(m_current, "expected a supported property type");
                Advance();
                continue;
            }
            ShaderInfoProperty property;
            property.type = m_current.text;
            Advance();
            if (m_current.kind != TokenKind::Identifier) {
                Error(m_current, "expected a property name");
                SkipToPropertyBoundary();
                continue;
            }
            property.name = m_current.text;
            const Token nameToken = m_current;
            Advance();
            if (!Consume(TokenKind::Equals, "expected '=' after property name")) {
                SkipToPropertyBoundary();
                continue;
            }
            property.defaultValue = ParsePropertyDefault(property.type);
            while (m_current.kind == TokenKind::Identifier && !IsPropertyType(m_current.text)) {
                const Token attribute = m_current;
                Advance();
                if (attribute.text == "HDR") {
                    property.hdr = true;
                } else if (attribute.text == "Range") {
                    property.range = ParseRange();
                } else {
                    Warning(attribute, "unknown property attribute '" + attribute.text + "'");
                    if (m_current.kind == TokenKind::LeftParen)
                        SkipBalanced(TokenKind::LeftParen, TokenKind::RightParen);
                }
            }
            if (property.range && property.type != "Float" && property.type != "Int") {
                Error(nameToken, "Range is only valid on Float and Int properties");
                property.range.reset();
            }
            if (!names.insert(property.name).second)
                Error(nameToken, "duplicate property '" + property.name + "'");
            property.source = {begin.begin, m_previous.end};
            m_document.properties.push_back(std::move(property));
            if (m_current.kind == TokenKind::Semicolon)
                Advance();
        }
        Consume(TokenKind::RightBrace, "unterminated Properties block");
    }

    std::string ParsePropertyDefault(std::string_view type)
    {
        if (m_current.kind == TokenKind::LeftBracket) {
            const size_t begin = m_current.begin.offset;
            int depth = 0;
            Token end = m_current;
            do {
                if (m_current.kind == TokenKind::LeftBracket)
                    ++depth;
                else if (m_current.kind == TokenKind::RightBracket)
                    --depth;
                end = m_current;
                Advance();
            } while (depth > 0 && m_current.kind != TokenKind::End);
            if (depth != 0)
                Error(end, "unterminated array property default");
            return std::string(m_source.substr(begin, end.end.offset - begin));
        }
        if (m_current.kind == TokenKind::Identifier || m_current.kind == TokenKind::String ||
            m_current.kind == TokenKind::Number) {
            const Token value = m_current;
            Advance();
            return value.text;
        }
        Error(m_current, "expected a default value for " + std::string(type) + " property");
        return {};
    }

    std::optional<ShaderInfoRangeAttribute> ParseRange()
    {
        if (!Consume(TokenKind::LeftParen, "expected '(' after Range"))
            return std::nullopt;
        const auto minimum = Scalar("Range minimum");
        if (!Consume(TokenKind::Comma, "expected ',' between Range bounds"))
            return std::nullopt;
        const auto maximum = Scalar("Range maximum");
        Consume(TokenKind::RightParen, "expected ')' after Range bounds");
        if (!minimum || !maximum)
            return std::nullopt;

        const auto parseBound = [&](const Token &token, std::string_view label) -> std::optional<double> {
            double value = 0.0;
            const auto result = std::from_chars(token.text.data(), token.text.data() + token.text.size(), value);
            if (result.ec != std::errc{} || result.ptr != token.text.data() + token.text.size()) {
                Error(token, std::string(label) + " must be a finite number");
                return std::nullopt;
            }
            return value;
        };
        const auto minimumValue = parseBound(*minimum, "Range minimum");
        const auto maximumValue = parseBound(*maximum, "Range maximum");
        if (!minimumValue || !maximumValue)
            return std::nullopt;
        if (*minimumValue >= *maximumValue) {
            Error(*maximum, "Range maximum must be greater than its minimum");
            return std::nullopt;
        }
        return ShaderInfoRangeAttribute{*minimumValue, *maximumValue};
    }

    void ParseVaryings(std::vector<ShaderInfoVarying> &target, std::string_view blockName)
    {
        if (!Consume(TokenKind::LeftBrace, "expected '{' after " + std::string(blockName)))
            return;
        std::unordered_set<std::string> names;
        while (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            ShaderInfoVarying varying;
            const Token begin = m_current;
            if (m_current.kind == TokenKind::Identifier && IsInterpolation(m_current.text)) {
                varying.interpolation = m_current.text;
                Advance();
            }
            if (m_current.kind != TokenKind::Identifier || !IsPropertyType(m_current.text)) {
                Error(m_current, "expected a supported varying type");
                Advance();
                continue;
            }
            varying.type = m_current.text;
            Advance();
            if (m_current.kind != TokenKind::Identifier) {
                Error(m_current, "expected a varying name");
                SkipToPropertyBoundary();
                continue;
            }
            varying.name = m_current.text;
            const Token nameToken = m_current;
            Advance();
            while (m_current.kind == TokenKind::Identifier && !IsPropertyType(m_current.text) &&
                   !IsInterpolation(m_current.text)) {
                const Token attribute = m_current;
                Advance();
                std::string value;
                if (m_current.kind == TokenKind::LeftParen) {
                    Advance();
                    if (auto scalar = Scalar(attribute.text))
                        value = scalar->text;
                    Consume(TokenKind::RightParen, "expected ')' after " + attribute.text);
                } else if (auto scalar = Scalar(attribute.text)) {
                    value = scalar->text;
                }
                if (attribute.text == "Semantic")
                    varying.semantic = value;
                else if (attribute.text == "Space")
                    varying.space = value;
                else
                    Warning(attribute, "unknown varying attribute '" + attribute.text + "'");
            }
            if (!names.insert(varying.name).second)
                Error(nameToken, "duplicate varying '" + varying.name + "'");
            varying.source = {begin.begin, m_previous.end};
            target.push_back(std::move(varying));
            if (m_current.kind == TokenKind::Semicolon)
                Advance();
        }
        Consume(TokenKind::RightBrace, "unterminated " + std::string(blockName) + " block");
    }

    void ParseList(std::vector<std::string> &target, std::string_view field)
    {
        if (!Consume(TokenKind::LeftBracket, "expected '[' after " + std::string(field)))
            return;
        std::unordered_set<std::string> values(target.begin(), target.end());
        while (m_current.kind != TokenKind::RightBracket && m_current.kind != TokenKind::End) {
            const auto value = Scalar(field);
            if (!value)
                break;
            if (!values.insert(value->text).second)
                Warning(*value, "duplicate " + std::string(field) + " value '" + value->text + "'");
            else
                target.push_back(value->text);
            if (m_current.kind == TokenKind::Comma) {
                Advance();
            } else if (m_current.kind != TokenKind::RightBracket) {
                Error(m_current, "expected ',' or ']' in " + std::string(field));
                break;
            }
        }
        Consume(TokenKind::RightBracket, "unterminated " + std::string(field) + " list");
    }

    void ParseEntry(const Token &begin)
    {
        const auto role = Scalar("Entry role");
        const auto function = Scalar("Entry function");
        if (!role || !function)
            return;
        const auto duplicate = std::find_if(m_document.entries.begin(), m_document.entries.end(),
                                            [&](const ShaderInfoEntry &entry) { return entry.role == role->text; });
        if (duplicate != m_document.entries.end())
            Error(*role, "duplicate Entry role '" + role->text + "'");
        m_document.entries.push_back({role->text, function->text, {begin.begin, function->end}});
    }

    void SkipUnknownValue()
    {
        if (m_current.kind == TokenKind::LeftBrace) {
            SkipBalanced(TokenKind::LeftBrace, TokenKind::RightBrace);
        } else if (m_current.kind == TokenKind::LeftBracket) {
            SkipBalanced(TokenKind::LeftBracket, TokenKind::RightBracket);
        } else if (m_current.kind != TokenKind::RightBrace && m_current.kind != TokenKind::End) {
            Advance();
        }
    }

    void SkipBalanced(TokenKind open, TokenKind close)
    {
        int depth = 0;
        do {
            if (m_current.kind == open)
                ++depth;
            else if (m_current.kind == close)
                --depth;
            Advance();
        } while (depth > 0 && m_current.kind != TokenKind::End);
    }

    void SkipToPropertyBoundary()
    {
        while (m_current.kind != TokenKind::End && m_current.kind != TokenKind::RightBrace &&
               m_current.kind != TokenKind::Semicolon &&
               !(m_current.kind == TokenKind::Identifier && IsPropertyType(m_current.text)))
            Advance();
        if (m_current.kind == TokenKind::Semicolon)
            Advance();
    }

    void Validate()
    {
        if (m_document.name.empty())
            Error({TokenKind::Invalid, {}, m_document.declaration.begin, m_document.declaration.begin},
                  "ShaderInfo requires Name");
    }

    void DetectDuplicateDeclarations()
    {
        while (m_current.kind != TokenKind::End) {
            if (m_current.kind == TokenKind::Identifier &&
                (m_current.text == "ShaderInfo" || m_current.text == "ShadingModelInfo")) {
                Error(m_current, "a shader asset may contain exactly one ShaderInfo or ShadingModelInfo declaration");
                return;
            }
            Advance();
        }
    }

    std::string_view m_source;
    Lexer m_lexer;
    Token m_current;
    Token m_previous;
    ShaderInfoDocument m_document;
};
} // namespace

bool ShaderInfoDocument::IsValid() const noexcept
{
    return foundDeclaration &&
           std::none_of(diagnostics.begin(), diagnostics.end(), [](const ShaderInfoDiagnostic &diagnostic) {
               return diagnostic.severity == ShaderInfoDiagnosticSeverity::Error;
           });
}

ShaderInfoDocument ParseShaderInfo(std::string_view source)
{
    return Parser(source).Parse();
}

std::string StripShaderInfoDeclaration(std::string_view source, const ShaderInfoDocument &document)
{
    std::string result(source);
    if (!document.foundDeclaration || document.declaration.end.offset <= document.declaration.begin.offset ||
        document.declaration.end.offset > result.size())
        return result;
    for (size_t offset = document.declaration.begin.offset; offset < document.declaration.end.offset; ++offset) {
        if (result[offset] != '\n' && result[offset] != '\r')
            result[offset] = ' ';
    }
    return result;
}

ShaderEntryPointSet DetectShaderEntryPoints(std::string_view source)
{
    ShaderEntryPointSet entries;
    Lexer lexer(source);
    Token returnType = lexer.Next();
    Token name = lexer.Next();
    Token open = lexer.Next();
    while (open.kind != TokenKind::End) {
        if (returnType.kind == TokenKind::Identifier && name.kind == TokenKind::Identifier &&
            open.kind == TokenKind::LeftParen) {
            entries.main = entries.main || (returnType.text == "void" && name.text == "main");
            entries.surface = entries.surface || (returnType.text == "void" && name.text == "surface");
            entries.vertex = entries.vertex || ((returnType.text == "void" || returnType.text == "VertexOutput") &&
                                                name.text == "vertex");
        }
        returnType = name;
        name = open;
        open = lexer.Next();
    }
    return entries;
}

std::string RewriteShaderEntryPoint(std::string_view source, std::string_view expectedReturnType,
                                    std::string_view entryPoint, std::string_view replacement)
{
    Lexer lexer(source);
    Token returnType = lexer.Next();
    Token name = lexer.Next();
    Token open = lexer.Next();
    while (open.kind != TokenKind::End) {
        if (returnType.kind == TokenKind::Identifier && returnType.text == expectedReturnType &&
            name.kind == TokenKind::Identifier && name.text == entryPoint && open.kind == TokenKind::LeftParen) {
            std::string rewritten(source);
            rewritten.replace(name.begin.offset, name.end.offset - name.begin.offset, replacement);
            return rewritten;
        }
        returnType = name;
        name = open;
        open = lexer.Next();
    }
    return std::string(source);
}

std::optional<ShaderSourceLocation> FindShaderLayoutDeclaration(std::string_view source)
{
    Lexer lexer(source);
    Token token = lexer.Next();
    while (token.kind != TokenKind::End) {
        if (token.kind == TokenKind::Identifier && token.text == "layout") {
            const ShaderSourceLocation location = token.begin;
            token = lexer.Next();
            if (token.kind == TokenKind::LeftParen)
                return location;
            continue;
        }
        token = lexer.Next();
    }
    return std::nullopt;
}

} // namespace infernux

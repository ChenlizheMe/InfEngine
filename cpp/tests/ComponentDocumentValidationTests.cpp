#include <function/scene/ComponentDocumentValidation.h>

#include <cassert>
#include <iostream>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace
{

using infernux::component_document_validation::ValidateComponentDocument;
using nlohmann::json;

json MakeDocument()
{
    return {
        {"type", "Example"},
        {"enabled", true},
        {"execution_order", 0},
        {"value", 7},
    };
}

void VerifyUnknownFieldsAreRejected()
{
    auto document = MakeDocument();
    document["removed_field"] = "old asset payload";
    bool rejected = false;
    try {
        ValidateComponentDocument(document, "Example", {"value"});
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}

void VerifyEnvelopeAndCurrentFieldsRemainStrict()
{
    auto document = MakeDocument();
    document["type"] = "Other";
    bool rejected = false;
    try {
        ValidateComponentDocument(document, "Example", {"value"});
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);

    document = MakeDocument();
    document.erase("value");
    rejected = false;
    try {
        ValidateComponentDocument(document, "Example", {"value"});
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}

} // namespace

int main()
{
    VerifyUnknownFieldsAreRejected();
    VerifyEnvelopeAndCurrentFieldsRemainStrict();
    std::cout << "Component document validation tests passed\n";
    return 0;
}
